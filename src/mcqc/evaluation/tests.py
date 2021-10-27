import abc
import enum
import math
import os
from typing import Dict, List
import torchvision
import shutil

from tqdm import tqdm, trange
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from mcqc.config import Config
from compressai._CXX import pmf_to_quantized_cdf
from compressai import ans

from mcqc.evaluation.metrics import MsSSIM, psnr as validatePSNR

def deTrans(image):
    eps = 1e-3
    max_val = 255
    # [0, 1] to [0, 255]
    return (image * (max_val + 1.0 - eps)).clamp(0.0, 255.0).byte()

class Test(abc.ABC):
    def __init__(self, config: Config, encoder: nn.Module, decoder: nn.Module, preProcess: nn.Module, postProcess: nn.Module, device: str, **kwArgs):
        super().__init__()
        self._config = config
        self._encoder = encoder
        self._decoder = decoder
        self._preProcess = preProcess
        self._postProcess = postProcess
        self._device = device
        self._encoder.eval().to(self._device)
        self._decoder.eval().to(self._device)
        self._preProcess.eval().to(self._device)
        self._postProcess.eval().to(self._device)
        torch.autograd.set_grad_enabled(False)
        setattr(self, "test", torch.inference_mode()(self.test))

    @abc.abstractmethod
    def test(self) -> Dict[str, float]:
        raise NotImplementedError


class Speed(Test):
    def __init__(self, **kwArgs) -> None:
        super().__init__(**kwArgs)
        # same as kodak
        self._testInput = torch.rand(6, 3, 768, 512).to(self._device)
        self._warmupStep = 10
        self._evalStep = 100

    def test(self):
        x, cAndPadding = self._preProcess(self._testInput)
        # warmup
        for _ in trange(self._warmupStep):
            b, cAndPadding = self._encoder(x, cAndPadding)
            self._decoder(b, cAndPadding)
        torch.cuda.synchronize()

        startEvent = torch.cuda.Event(enable_timing=True)
        endEvent = torch.cuda.Event(enable_timing=True)


        startEvent.record()
        # test encoder
        for _ in trange(self._evalStep):
            self._encoder(x, cAndPadding)
        endEvent.record()
        torch.cuda.synchronize()
        encoderMs = startEvent.elapsed_time(endEvent) / (self._evalStep * len(self._testInput))

        startEvent = torch.cuda.Event(enable_timing=True)
        endEvent = torch.cuda.Event(enable_timing=True)

        startEvent.record()
        # test encoder
        for _ in trange(self._evalStep):
            self._decoder(b, cAndPadding)
        endEvent.record()
        torch.cuda.synchronize()
        decoderMs = startEvent.elapsed_time(endEvent) / (self._evalStep * len(self._testInput))

        return {"encoderForwardTime": encoderMs, "decoderForwardTime": decoderMs}


class Performance(Test):
    def __init__(self, dataset: Dataset, **kwArgs):
        super().__init__(**kwArgs)
        self._dataLoader = DataLoader(dataset, pin_memory=True)
        self._ssim = MsSSIM(sizeAverage=False).to(self._device)

    def test(self):
        shutil.rmtree("ckpt/images", ignore_errors=True)
        os.makedirs("ckpt/images", exist_ok=True)
        ssims = list()
        psnrs = list()
        bs = list()
        pixels = list()
        images = list()
        for i, x in enumerate(tqdm(self._dataLoader)):
            x = x.to(self._device, non_blocking=True)
            minLength = 128
            _, _, h, w = x.shape

            wCrop = w - math.floor(w / minLength) * minLength
            hCrop = h - math.floor(h / minLength) * minLength
            cropLeft = wCrop // 2
            cropRight = wCrop - cropLeft
            cropTop = hCrop // 2
            cropBottom = hCrop - cropTop

            if cropBottom == 0:
                cropBottom = -h
            if cropRight == 0:
                cropRight = -w

            x = x[:, :, cropTop:(-cropBottom), cropLeft:(-cropRight)]

            _, _, h, w = x.shape
            xPadded, cAndPadding = self._preProcess(x)
            # list of [1, ?, ?, m]
            b, cAndPadding = self._encoder(xPadded, cAndPadding)
            y, cAndPadding = self._decoder(b, cAndPadding)
            y = self._postProcess(y, cAndPadding)
            x, y = deTrans(x), deTrans(y)

            ssims.append(float(-10 * (1.0 - self._ssim(x.float(), y.float())).log10()))
            psnrs.append(float(validatePSNR(x.float(), y.float())))
            # list of [m, ?]
            bs.append(b)
            pixels.append(h * w)
            images.append(y[0].byte().cpu())

        cdfs = self._getCDFs(bs)

        binaries = list()
        bpps = list()

        for i, (b, pixel, ssim, psnr, image) in enumerate(zip(tqdm(bs), pixels, ssims, psnrs, images)):
            binary, bpp = self._compress(cdfs, b, pixel)
            binaries.append(binary)
            bpps.append(bpp)
            torchvision.io.write_png(image, f"ckpt/images/test_SSIM_{ssim:2.2f}_PSNR_{psnr:2.2f}_bpp_{bpp:.4f}_{i}.png")
        return {"ssim": sum(ssims) / len(ssims), "psnr": sum(psnrs) / len(psnrs), "bpp": sum(bpps) / len(bpps)}

    def _getCDFs(self, bs: List[List[torch.Tensor]]):
        eachLevelEachPartCode = list(list(list() for _ in range(self._config.Model.m)) for _ in range(len(self._config.Model.k)))

        cdfs = list(list(object() for _ in range(self._config.Model.m)) for _ in range(len(self._config.Model.k)))

        for b in bs:
            #       [n, h, w, m]
            for lv, level in enumerate(b):
                #      [n, h, w]
                for m, part in enumerate(level.permute(3, 0, 1, 2)):
                    # n * [variable length] code
                    eachLevelEachPartCode[lv][m].extend(part.reshape(len(part), -1))
        for lv, levels in enumerate(eachLevelEachPartCode):
            for m, part in enumerate(levels):
                # [n * varLength] codes in level l part m.
                allCodes = torch.cat(part)
                prob = self._calculateFreq(allCodes.flatten(), self._config.Model.k[lv])
                cdf = pmf_to_quantized_cdf(prob.tolist(), 16)
                cdfs[lv][m] = cdf
        return cdfs


    def _compress(self, cdfs: List[List[object]], codes: List[torch.Tensor], totalPixels: int):
        encoder = ans.RansEncoder()

        binaries = list()

        for lv in range(len(self._config.Model.k)):
            # List of m cdfs
            cdf = cdfs[lv]
            # [1, h, w, m]
            code = codes[lv]
            # [m, h*w]
            code = code[0].permute(2, 0, 1).reshape(code.shape[-1], -1)
            # cdf = list()
            # for c in code:
            #     prob = self._calculateFreq(c.flatten(), self._config.Model.k[lv])
            #     cdfOfLv = pmf_to_quantized_cdf(prob.tolist(), 16)
            #     cdf.append(cdfOfLv)
            index = torch.arange(code.shape[0])[:, None].expand_as(code).flatten().int().tolist()
            cdfSize = [self._config.Model.k[lv] + 2] * self._config.Model.m
            offset = torch.zeros_like(code).flatten().int().tolist()
            # params: List of symbols, List of indices of pdfs, List of pdfs, List of upper-bounds, List of offsets
            # [0, 1, 2, 3], [0, 0, 1, 1], [[xx, xx, xx, xx], [xx, xx, xx, xx]], [4, 4, 4, 4], [0, 0, 0, 0]
            binary: str = encoder.encode_with_indexes(code.flatten().int().tolist(), index, cdf, cdfSize, offset)
            # [M, h, w] binary
            binaries.append(binary)
        # # binary: 1 byte per word
        # # N * [binaries]
        total = 8 * sum(len(binary) for binary in binaries)
        bpp = float(total) / totalPixels

        # perfect = sum(bits) / sum(totalPixels)
        # self._logger.info("Estimate \"perfect\" BPP: %.4f", perfect)
        return binaries, bpp


    def _decompressAndCheck(self, rawCodes: List[List[List[torch.Tensor]]], binaries: List[str], cdfs: List[List[float]]):
        decoder = ans32.RansDecoder()

        for lv, levels in enumerate(rawCodes):
            images = list()

        for lv, levels in enumerate(rawCodes):
            images = list()
            cdfs = list()
            for part in levels:
                # N * [-1] all code of images at level i, group m
                c = torch.cat(part)
                pixels = len(c)
                prob = self._calculateFreq(c.flatten(), self._config.Model.k[lv])
                estimateEntropy = prob.log2()
                estimateEntropy[estimateEntropy == float("-inf")] = 0
                estimateEntropy = -(prob * estimateEntropy).sum().item()
                bits.append(estimateEntropy * pixels)
                cdf = pmf_to_quantized_cdf(prob.tolist(), 16)
                cdfs.append(cdf)
                images.append(part)
            # codePerImage: M * [Tensor of [h * w]]
            for i, codePerImage in enumerate(zip(*images)):
                # [M, h * w]
                codePerImage = torch.stack(codePerImage, 0)
                indices = torch.arange(codePerImage.shape[0])[:, None].expand_as(codePerImage).flatten().int().tolist()
                cdfSizes = [self._config.Model.k[lv] + 2] * self._config.Model.m
                offsets = torch.zeros_like(codePerImage).flatten().int().tolist()
                # params: List of symbols, List of indices of pdfs, List of pdfs, List of upper-bounds, List of offsets
                # [0, 1, 2, 3], [0, 0, 1, 1], [[xx, xx, xx, xx], [xx, xx, xx, xx]], [4, 4, 4, 4], [0, 0, 0, 0]
                binary: str = encoder.encode_with_indexes(codePerImage.flatten().int().tolist(), indices, cdfs, cdfSizes, offsets)
                # [M, h, w] binary
                compressed.append(binary)
                bpps.append(8 * len(binary) / totalPixels[i])
        for binary, raw in zip(binaries, rawCodes):
            m, h, w = raw.shape
            code: List[int] = decoder.decode_with_indexes(binary, torch.arange(m)[:, None, None].expand(m, h, w).flatten().int().tolist(), cdfs, [self._config.Model.k] * m, torch.zeros(m, h, w).flatten().int().tolist())
            code = torch.tensor(code, dtype=torch.long).reshape(m, h, w)
            print(code)
            print(raw)
            input()
            if torch.any(raw != code):
                raise ValueError("Decompress failed, decoded b not equals to raw b.")



    def _calculateFreq(self, code: torch.Tensor, k):
        count = torch.bincount(code.long(), minlength=k)
        return count / code.numel()
