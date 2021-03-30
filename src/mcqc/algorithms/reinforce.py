from typing import Type, Callable, Iterator
from logging import Logger

import numpy as np
import torch
from torch import nn
from torch.distributions import Categorical
from cfmUtils.saver import Saver
from cfmUtils.vision.colorSpace import rgb2hsv, hsv2rgb
from pytorch_msssim import ms_ssim

from mcqc.algorithms.algorithm import Algorithm
from mcqc.evaluation.helpers import evalSSIM, psnr
from mcqc.models.whole import Whole
from mcqc import Consts, Config

def _ssimExp(source, target, datarange):
    return (2.7182818284590452353602874713527 - ms_ssim(source, target, data_range=datarange).exp()) / (1.7182818284590452353602874713527)


class Reinforce(Algorithm):
    def __init__(self, config: Config, model: Whole, device: str, optimizer: Callable[[Iterator[nn.Parameter]], torch.optim.Optimizer], scheduler: Callable[[torch.optim.Optimizer], torch.optim.lr_scheduler._LRScheduler], saver: Saver, continueTrain: bool, logger: Logger):
        super().__init__()
        self._model = model
        if device == "cuda" and torch.cuda.device_count() > 1:
            self._model = nn.DataParallel(self._model.to(device))
        else:
            self._model = self._model.to(device)
        self._device = device
        # self._optimizerD = optimizer(1e-5, self._model.module._discriminator.parameters(), 0)
        self._optimizer = optimizer(config.LearningRate, self._model.parameters(), 0)
        # self._schedulerD = scheduler(self._optimizerD)
        self._scheduler = scheduler(self._optimizer)
        self._saver = saver
        self._logger = logger
        self._config = config
        self._continue = continueTrain

        self._eps = 0.2

    @staticmethod
    def _deTrans(imaage):
        return ((imaage * 0.5 + 0.5) * 255).clamp(0.0, 255.0).byte()

    def run(self, trainLoader: torch.utils.data.DataLoader, testLoader: torch.utils.data.DataLoader):
        initTemp = 1.0
        minTemp = 0.1
        step = 0
        flag = False
        count = 0
        regCoeff = self._config.Coef.reg
        dB = 0.0
        target = 17.0
        cv = 1e-8
        maxCV = 0.1

        if self._continue:
            loaded = self._saver.load(self._saver.SavePath, self._logger, model=self._model, optim=self._optimizer, schdrG=self._scheduler, step=step, temp=initTemp)
            initTemp = loaded["temp"]
            step = loaded["step"]

        dB = self._eval(testLoader, step)

        for i in range(self._config.Epoch):
            self._model.train()
            for images in trainLoader:
                images = images.to(self._device, non_blocking=True)
                if step % 2 == 0:
                    with torch.no_grad():
                        ssimLoss, l1l2Loss, rewards, restored, codes, latents, oldNegLogPs, _, quantizeds, oldValues = self._model(images)
                    self._optimizer.zero_grad()
                    _, negLogPs, values = self._model(images, codes)
                    for oldNegLogP, negLogP, oldValue, value, reward in zip(oldNegLogPs, negLogPs, oldValues, values, rewards):
                        ratio = torch.exp(oldNegLogP - negLogP)
                        advantage = reward - value
                        surrogate1 = -ratio * advantage
                        surrogate2 = -ratio.clamp(1.0 - self._eps, 1.0 + self._eps) * advantage
                        policyLoss = torch.max(surrogate1, surrogate2)

                        valueClipped = oldValue + (value - oldValue).clamp(-self._eps, self._eps)
                        surrogate1 = (reward - value) ** 2
                        surrogate2 = (reward - valueClipped) ** 2
                        valueLoss = torch.max(surrogate1, surrogate2)

                    (policyLoss + valueLoss).mean().backward()
                    # torch.nn.utils.clip_grad_norm_(self._model.parameters(), max_norm=0.5)
                    self._optimizer.step()
                    self._saver.add_scalar("loss/policyLoss", policyLoss.mean(), global_step=step)
                    self._saver.add_scalar("loss/valueLoss", valueLoss.mean(), global_step=step)
                else:
                    self._optimizer.zero_grad()
                    ssimLoss, l1l2Loss, reward, restored, codes, latents, oldNegLogP, logits, quantizeds, oldValue = self._model(images)
                    (self._config.Coef.ssim * ssimLoss + self._config.Coef.l1l2 * l1l2Loss).mean().backward()
                    # torch.nn.utils.clip_grad_norm_(self._model.parameters(), max_norm=10.0)
                    self._optimizer.step()
                    # self._saver.add_scalar("loss/gLoss", gLoss.mean(), global_step=step)
                    self._saver.add_scalar("loss/ssimLoss", ssimLoss.mean(), global_step=step)
                    self._saver.add_scalar("loss/l1l2Loss", l1l2Loss.mean(), global_step=step)
                if (step + 1) % 100 == 0:
                    self._saver.add_images("train/raw", self._deTrans(images), global_step=step)
                    self._saver.add_images("train/res", self._deTrans(restored), global_step=step)
                    self._saver.add_histogram("code", codes[0].reshape(-1), global_step=step)
                if (step + 1) % 1000 == 0:
                    dB = self._eval(testLoader, step)
                    if dB > target:
                        cv = min(cv * 10, maxCV)
                        # target += 2.0
                        # count = 0
                        self._logger.info("Re-init codebook and change target to %d", int(target))
                    self._saver.save(self._logger, model=self._model, optim=self._optimizer, schdr=self._scheduler, step=step+1, temp=initTemp)
                    self._logger.info("%3dk steps complete, update: LR = %.2e, T = %.2e, count = %d", (step + 1) // 1000, self._scheduler.get_last_lr()[0], initTemp, count)
                if (step + 1) % 10000 == 0 and step > 100000 and step < 130000:
                    # self._schedulerD.step()
                    self._scheduler.step()
                    self._logger.info("reduce lr")
                # initTemp = max(initTemp * 0.9999, minTemp)
                step += 1
                # cv *= min(cv * 1.0001, maxCV)
                # mixin *= 0.9999

    @torch.no_grad()
    def _eval(self, dataLoader: torch.utils.data.DataLoader, step: int):
        self._model.eval()
        ssims = list()
        psnrs = list()
        if isinstance(self._model, nn.DataParallel):
            model = self._model.module._compressor
        else:
            model = self._model._compressor
        model = model.cuda()
        bs = list()
        zs = list()
        for raw in dataLoader:
            raw = raw.to(self._device, non_blocking=True)

            # restored, _, _, _, _ = self._model(raw, 0.5, True, 0.0)
            latents = model._encoder(raw)
            b, z = model._quantizer.encode(latents)
            bs.append(b[0].detach().cpu())
            zs.append(z[0].detach().cpu())

            quantized = model._quantizer.decode(b)
            restored = model._decoder(quantized)
            raw = self._deTrans(raw)
            restored = self._deTrans(restored)
            ssims.append(evalSSIM(restored.detach(), raw.detach(), True))
            psnrs.append(psnr(restored.detach(), raw.detach()))
        ssims = torch.cat(ssims, 0)
        psnrs = torch.cat(psnrs, 0)
        np.save("b.npy", torch.cat(bs, 0).cpu().numpy())
        # np.save("c.npy", self._model.module.codebook[0].weight.detach().cpu().numpy())
        np.save("z.npy", torch.cat(zs, 0).cpu().numpy())
        # exit()
        self._logger.info("MS-SSIM: %2.2fdB", ssims.mean())
        self._logger.info("   PSNR: %2.2fdB", psnrs.mean())
        self._saver.add_images("eval/res", restored, global_step=step)
        del bs, zs
        return float(psnrs.mean())

    def _loss(self, step, images, restored, codes, latents, logits, quantizeds):
        # hsvR, rgbR = torch.chunk(restored, 2, 1)
        # combined = torch.cat([images, hsvImages], axis=1)
        l2Loss = torch.nn.functional.mse_loss(restored, images) # + torch.nn.functional.mse_loss(hsvR, hsvImages)
        l1Loss = torch.nn.functional.l1_loss(restored, images) # + torch.nn.functional.l1_loss(hsvR, hsvImages)
        ssimLoss = 1 - ms_ssim((restored + 1), (images + 1), data_range=2.0) # - ms_ssim((hsvR + 1), (hsvImages + 1), data_range=2.0)
        self._saver.add_scalar("loss/l2", l2Loss, global_step=step)
        self._saver.add_scalar("loss/l1", l1Loss, global_step=step)
        self._saver.add_scalar("loss/ssim", ssimLoss, global_step=step)

        # transformerL2 = list()
        # transformerL1 = list()
        # commitL2 = list()
        # commitL1 = list()
        # for q, t in zip(quantizeds, latents):
        #     transformerL2.append(torch.nn.functional.mse_loss(q, t.detach()))
        #     transformerL1.append(torch.nn.functional.l1_loss(q, t.detach()))
        #     commitL2.append(torch.nn.functional.mse_loss(t, q.detach()))
        #     commitL1.append(torch.nn.functional.l1_loss(t, q.detach()))
        # transformerL2 = sum(transformerL2)
        # transformerL1 = sum(transformerL1)
        # commitL2 = sum(commitL2)
        # commitL1 = sum(commitL1)
        # self._saver.add_scalar("loss/tl2", transformerL2, global_step=step)
        # self._saver.add_scalar("loss/tl1", transformerL1, global_step=step)
        # self._saver.add_scalar("stat/lnorm", (t**2).mean(), global_step=step)
        # self._saver.add_scalar("stat/lvar", t.std(), global_step=step)
        # self._saver.add_scalar("stat/qnorm", (q**2).mean(), global_step=step)
        # self._saver.add_scalar("stat/qvar", q.std(), global_step=step)

        # ssimLoss = _ssimExp((rgbR + 1), (images + 1), 2.0) + _ssimExp((hsvR + 1), (hsvImages + 1), 2.0)


        # qe = quantizationError(highOrders, softs, hards)
        # self._saver.add_scalar("loss/qe", qe, global_step=step)

        # klLoss = torch.nn.functional.kl_div(torch.nn.functional.log_softmax(logitsConsistency, -1), torch.nn.functional.log_softmax(logitsCompressed.detach(), -1), reduction="batchmean", log_target=True)
        return ssimLoss \
             + 0.1 * (l1Loss + l2Loss) \
            #  + regs \
            #  + qe \
             # + 1.0 * (transformerL1 + transformerL2) \
             # + 1e-2 * (commitL1 + commitL2), \
             # bool(transformerL2 < 0.05)
           # + 1e-6 * klLoss \


def spatialKL(logit):
    lenLogits = logit.shape[1] * logit.shape[2]
    logit = logit.reshape(logit.shape[0], lenLogits, -1)
    logProb = torch.nn.functional.log_softmax(logit, -1)
    randIdx1 = torch.randperm(lenLogits)
    randIdx2 = torch.randperm(lenLogits)
    shuffle1 = logProb[:, randIdx1]
    shuffle2 = logProb[:, randIdx2]
    return torch.nn.functional.kl_div(shuffle1, shuffle2, reduction="batchmean", log_target=True)


def quantizationError(raws, softs, hards):
    softQE = list()
    hardQE = list()
    jointQE = list()
    for r, s, h in zip(raws, softs, hards):
        softQE.append(torch.nn.functional.mse_loss(s, r))
        hardQE.append(torch.nn.functional.mse_loss(h, r))
        jointQE.append(torch.nn.functional.mse_loss(s, h))
    return sum(softQE) + 0.1 * sum(jointQE) + sum(hardQE)


def compute_penalties(logits, individual_entropy_coeff=0.0, allowed_entropy=0.0, global_entropy_coeff=0.0,
                      cv_coeff=0.0, eps=1e-9):
    """
    Computes typical regularizers for gumbel-softmax quantization
    Regularization is of slight help when performing hard quantization, but it isn't critical
    :param logits: tensor [batch_size, ..., codebook_size]
    :param individual_entropy_coeff: penalizes mean individual entropy
    :param allowed_entropy: does not penalize individual_entropy if it is below this value
    :param cv_coeff: penalizes squared coefficient of variation
    :param global_entropy_coeff: coefficient for entropy of mean probabilities over batch
        this value should typically be negative (e.g. -1), works similar to cv_coeff
    """
    p = torch.softmax(logits, dim=-1)
    # logp = torch.log_softmax(logits, dim=-1)
    # [batch_size, ..., codebook_size]

    # individual_entropy_values = -torch.sum(p * logp, dim=-1)
    # clipped_entropy = torch.nn.functional.relu(allowed_entropy - individual_entropy_values + eps).mean()
    # individual_entropy = (individual_entropy_values.mean() - clipped_entropy).detach() + clipped_entropy

    # global_p = torch.mean(p, dim=0)  # [..., codebook_size]
    # global_logp = torch.logsumexp(logp, dim=0) - np.log(float(logp.shape[0]))  # [..., codebook_size]
    # global_entropy = -torch.sum(global_p * global_logp, dim=-1).mean()

    load = torch.mean(p, dim=0)  # [..., codebook_size]
    mean = load.mean()
    variance = torch.mean((load - mean) ** 2)
    cvPenalty = variance / (mean ** 2 + eps)
    return cv_coeff * cvPenalty