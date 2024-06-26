import math
from typing import Callable, Dict, List, Tuple, Union

import torch
from torch import nn
import torch.distributed as dist
import torch.nn.functional as F

from mcquic.modules.entropyCoder import EntropyCoder, PlainCoder, VariousMCoder
from mcquic.nn.base import LowerBound
from mcquic.utils.specification import CodeSize
from mcquic import Consts
from mcquic.nn.base import gumbelSoftmax
from mcquic.nn.gdn import GenDivNorm, InvGenDivNorm



class GradMultiply(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale):
        ctx.scale = scale
        res = x.new(x)
        return res


    @staticmethod
    def backward(ctx, grad):
        return grad * ctx.scale, None



class BaseQuantizer(nn.Module):
    def __init__(self, m: int, k: List[int]):
        super().__init__()
        self._entropyCoder = EntropyCoder(m, k)
        self._m = m
        self._k = k

    def encode(self, x: torch.Tensor) -> List[torch.Tensor]:
        raise NotImplementedError

    def decode(self, codes: List[torch.Tensor]) -> torch.Tensor:
        raise NotImplementedError

    @property
    def Codebooks(self) -> List[torch.Tensor]:
        raise NotImplementedError

    @property
    def CDFs(self):
        return self._entropyCoder.CDFs

    def reAssignCodebook(self) -> torch.Tensor:
        raise NotImplementedError

    def syncCodebook(self):
        raise NotImplementedError

    @property
    def NormalizedFreq(self):
        return self._entropyCoder.NormalizedFreq

    def compress(self, x: torch.Tensor) -> Tuple[List[torch.Tensor], List[List[bytes]], List[CodeSize]]:
        codes = self.encode(x)

        # List of binary, len = n, len(binaries[0]) = level
        binaries, codeSize = self._entropyCoder.compress(codes)
        return codes, binaries, codeSize

    def _validateCode(self, refCodes: List[torch.Tensor], decompressed: List[torch.Tensor]):
        for code, restored in zip(refCodes, decompressed):
            if torch.any(code != restored):
                raise RuntimeError("Got wrong decompressed result from entropy coder.")

    def decompress(self, binaries: List[List[bytes]], codeSize: List[CodeSize]) -> torch.Tensor:
        decompressed = self._entropyCoder.decompress(binaries, codeSize)
        # self._validateCode(codes, decompressed)
        return self.decode(decompressed)

class PlainQuantizer(BaseQuantizer):
    def __init__(self, m: int, k: List[int]):
        nn.Module.__init__(self)
        self._entropyCoder = PlainCoder(m, k)
        self._m = m
        self._k = k


class VariousMQuantizer(BaseQuantizer):
    def __init__(self, m: List[int], k: List[int]):
        nn.Module.__init__(self)
        self._entropyCoder = VariousMCoder(m, k)
        self._m = m
        self._k = k

# NOTE: You may notice the quantizer implemented here is different with README.md
#       After some tests, I find some strange behavior if `k` is not placed in the last dim.
#       Generally, although code is neat and output is same as here,
#         training with README's implementation will cause loss become suddenly NAN after a few epoches.
class _multiCodebookQuantization(nn.Module):
    def __init__(self, codebook: nn.Parameter, freqEMA: nn.Parameter):
        super().__init__()
        self._m, self._k, self._d = codebook.shape
        self._codebook = codebook
        self._bits = math.log2(self._k)
        self._scale = math.sqrt(self._k)
        self._temperature = nn.Parameter(torch.ones((self._m, 1, 1, 1)))
        self._bound = LowerBound(Consts.Eps)
        # [m, k]
        self._freqEMA = freqEMA

    def reAssignCodebook(self, freq: torch.Tensor)-> torch.Tensor:
        codebook = self._codebook.detach().clone()
        freq = freq.to(self._codebook.device).detach().clone()
        #       [k, d],        [k]
        for m, (codebookGroup, freqGroup) in enumerate(zip(self._codebook, freq)):
            neverAssignedLoc = freqGroup < Consts.Eps
            totalNeverAssigned = int(neverAssignedLoc.sum())
            # More than half are never assigned
            if totalNeverAssigned > self._k // 2:
                mask = torch.zeros((totalNeverAssigned, ), device=self._codebook.device)
                maskIdx = torch.randperm(len(mask))[self._k // 2:]
                # Random pick some never assigned loc and drop them.
                mask[maskIdx] = -1.
                freqGroup[neverAssignedLoc] = mask
                # Update
                neverAssignedLoc = (freqGroup < Consts.Eps) * (freqGroup > (-Consts.Eps))
                totalNeverAssigned = int(neverAssignedLoc.sum())
            argIdx = torch.argsort(freqGroup, descending=True)
            mostAssigned = codebookGroup[argIdx]
            # selectedIdx = mostAssigned[:totalNeverAssigned]
            codebook.data[m, neverAssignedLoc] = mostAssigned[:totalNeverAssigned]
        # [m, k] bool
        diff = ((codebook - self._codebook) ** 2).sum(-1) > 1e-4
        proportion = diff.flatten()
        self._codebook.data.copy_(codebook)
        return proportion

    def syncCodebook(self):
        # NOTE: don't directly broadcast parameters, this will mess up the autograd graph
        codebook = self._codebook.detach().clone()
        dist.broadcast(codebook, 0)
        self._codebook.data.copy_(codebook)

    def encode(self, x: torch.Tensor):
        # [n, m, h, w, k]
        distance = self._distance(x)
        # [n, m, h, w, k] -> [n, m, h, w]
        code = distance.argmin(-1)
        #      [n, m, h, w]
        return code

    # NOTE: ALREADY CHECKED CONSISTENCY WITH NAIVE IMPL.
    def _distance(self, x: torch.Tensor) -> torch.Tensor:
        n, _, h, w = x.shape
        # [n, m, d, h, w]
        x = x.reshape(n, self._m, self._d, h, w).contiguous()

        # [n, m, 1, h, w]
        x2 = (x ** 2).sum(2, keepdim=True)

        # codebook = GradMultiply.apply(self._codebook, 0.1)

        # [m, k, 1, 1]
        c2 = (self._codebook ** 2).sum(-1, keepdim=True)[..., None].contiguous()
        # [n, m, d, h, w] * [m, k, d] -sum-> [n, m, k, h, w]
        # inter = torch.einsum("nmdhw,mkd->nmkhw", x, self._codebook).contiguous()
        ######## NAIVE implemnetation ################
        # [n*m, hw, d]
        left = x.reshape(n*self._m, self._d, h*w).permute(0, 2, 1).contiguous()
        # [n*m, d, k]
        right = self._codebook.expand(n, self._m, self._k, self._d).reshape(n*self._m, self._k, self._d).permute(0, 2, 1).contiguous()
        # [n*m, hw, k]
        inter = torch.bmm(left, right)
        inter = inter.reshape(n, self._m, h, w, self._k).permute(0, 1, 4, 2, 3).contiguous()
        # [n, m, k, h, w]
        distance = x2 + c2 - 2 * inter
        # IMPORTANT to move k to last dim --- PLEASE SEE NOTE.
        # [n, m, h, w, k]
        return distance.permute(0, 1, 3, 4, 2).contiguous()

    def _logit(self, x: torch.Tensor) -> torch.Tensor:
        logit = -1 * self._distance(x)
        return logit / self._scale

    # def _permute(self, sample: torch.Tensor) -> torch.Tensor:
    #     if self._permutationRate < Consts.Eps:
    #         return sample
    #     # [n, h, w, m]
    #     needPerm = torch.rand_like(sample[..., 0]) < self._permutationRate
    #     randomed = F.one_hot(torch.randint(self._k, (needPerm.sum(), ), device=sample.device), num_classes=self._k).float()
    #     sample[needPerm] = randomed
    #     return sample.contiguous()

    def _randomDrop(self, logit):
        # if codeUsage == 0., then exponential = 12 (x**10 < freq, x=U(0,1)), if codeUsage == 1.0, then exponential = 1.
        codeUsage = (self._freqEMA > Consts.Eps).float().mean().clamp(0., 1.)
        # [n, m, h, w, k] < [m, 1, 1, k]
        randomMask = (torch.rand_like(logit) ** (-(self._bits - 1) * (codeUsage ** 2) + self._bits)) < self._freqEMA[:, None, None, ...]
        logit[randomMask] += -1e9
        return logit

    def _sample(self, x: torch.Tensor, temperature: float):
        # [n, m, h, w, k] * [m, 1, 1, 1]
        logit = self._logit(x) * self._bound(self._temperature)

        logit = self._randomDrop(logit)

        # It causes training unstable
        # leave to future tests.
        # add random mask to pick a different index.
        # [n, m, h, w]
        # needPerm = torch.rand_like(logit[..., 0]) < self._permutationRate * rateScale
        # target will set to zero (one of k) but don't break gradient
        # mask = F.one_hot(torch.randint(self._k, (needPerm.sum(), ), device=logit.device), num_classes=self._k).float() * logit[needPerm]
        # logit[needPerm] -= mask.detach()

        # NOTE: STE: code usage is very low; RelaxedOneHotCat: Doesn't have STE trick
        # So reverse back to F.gumbel_softmax
        # posterior = OneHotCategoricalStraightThrough(logits=logit / temperature)
        # [n, m, k, h, w]
        # sampled = posterior.rsample(())

        sampled = gumbelSoftmax(logit, temperature, True)

        # sampled = self._permute(sampled)

        # It causes training unstable
        # leave to future tests.
        # sampled = gumbelArgmaxRandomPerturb(logit, self._permutationRate * rateScale, temperature)
        return sampled, logit

    def forward(self, x: torch.Tensor):
        sample, logit = self._sample(x, 1.0)
        # [n, m, h, w, 1]
        code = logit.argmax(-1, keepdim=True)
        # [n, m, h, w, k]
        oneHot = torch.zeros_like(logit).scatter_(-1, code, 1).contiguous()
        # [n, m, h, w, k]
        return sample, code[..., 0].contiguous(), oneHot, logit


class _multiCodebookDeQuantization(nn.Module):
    def __init__(self, codebook: nn.Parameter):
        super().__init__()
        self._m, self._k, self._d = codebook.shape
        self._codebook = codebook
        self.register_buffer("_ix", torch.arange(self._m), persistent=False)

    def decode(self, code: torch.Tensor):
        # codes: [n, m, h, w]
        n, _, h, w = code.shape
        # [n, h, w, m]
        code = code.permute(0, 2, 3, 1).contiguous()
        # use codes to index codebook (m, k, d) ==> [n, h, w, m, k] -> [n, c, h, w]
        ix = self._ix.expand_as(code)
        # [n, h, w, m, d]
        indexed = self._codebook[ix, code]
        # [n, c, h, w]
        return indexed.reshape(n, h, w, -1).permute(0, 3, 1, 2).contiguous()

    # NOTE: ALREADY CHECKED CONSISTENCY WITH NAIVE IMPL.
    def forward(self, sample: torch.Tensor):
        n, _, h, w, _ = sample.shape
        # [n, m, h, w, k, 1], [m, 1, 1, k, d] -sum-> [n, m, h, w, d] -> [n, m, d, h, w] -> [n, c, h, w]
        # return torch.einsum("nmhwk,mkd->nmhwd", sample, self._codebook).contiguous().permute(0, 1, 4, 2, 3).contiguous().reshape(n, -1, h, w).contiguous()
        # [nm, hw, k]
        left = sample.reshape(n*self._m, h*w, self._k).contiguous()

        # codebook = GradMultiply.apply(self._codebook, 0.1)
        # [nm, k, d]
        right = self._codebook.expand(n, self._m, self._k, self._d).reshape(n*self._m, self._k, self._d).contiguous()
        # [nm, hw, d]
        result = torch.bmm(left, right)
        return result.reshape(n, self._m, h, w, self._d).permute(0, 1, 4, 2, 3).reshape(n, -1, h, w).contiguous()


class _quantizerEncoder(nn.Module):
    """
    Default structure:
    ```plain
        x [H, W]
        | `latentStageEncoder`
        z [H/2 , W/2] -------╮
        | `quantizationHead` | `latentHead`
        q [H/2, W/2]         z [H/2, w/2]
        |                    |
        ├-`subtract` --------╯
        residual for next level
    ```
    """

    def __init__(self, quantizer: _multiCodebookQuantization, dequantizer: _multiCodebookDeQuantization, latentStageEncoder: nn.Module, quantizationHead: nn.Module, latentHead: Union[None, nn.Module]):
        super().__init__()
        self._quantizer = quantizer
        self._dequantizer = dequantizer
        self._latentStageEncoder = latentStageEncoder
        self._quantizationHead = quantizationHead
        self._latentHead = latentHead

    @property
    def Codebook(self):
        return self._quantizer._codebook

    def syncCodebook(self):
        self._quantizer.syncCodebook()

    def reAssignCodebook(self, freq: torch.Tensor) -> torch.Tensor:
        return self._quantizer.reAssignCodebook(freq)

    def encode(self, x: torch.Tensor):
        # [h, w] -> [h/2, w/2]
        z = self._latentStageEncoder(x)
        code = self._quantizer.encode(self._quantizationHead(z))
        if self._latentHead is None:
            return None, code
        z = self._latentHead(z)
        #      ↓ residual,                         [n, m, h, w]
        return z - self._dequantizer.decode(code), code

    def forward(self, x: torch.Tensor):
        # [h, w] -> [h/2, w/2]
        z = self._latentStageEncoder(x)
        q, code, oneHot, logit = self._quantizer(self._quantizationHead(z))
        if self._latentHead is None:
            return q, None, code, oneHot, logit
        z = self._latentHead(z)
        #         ↓ residual
        return q, z - self._dequantizer(q), code, oneHot, logit

class _quantizerDecoder(nn.Module):
    """
    Default structure:
    ```plain
        q [H/2, W/2]            formerLevelRestored [H/2, W/2]
        | `dequantizaitonHead`  | `sideHead`
        ├-`add` ----------------╯
        xHat [H/2, W/2]
        | `restoreHead`
        nextLevelRestored [H, W]
    ```
    """

    def __init__(self, dequantizer: _multiCodebookDeQuantization, dequantizationHead: nn.Module, sideHead: Union[None, nn.Module], restoreHead: nn.Module):
        super().__init__()
        self._dequantizer =  dequantizer
        self._dequantizationHead = dequantizationHead
        self._sideHead = sideHead
        self._restoreHead =  restoreHead

    #                [n, m, h, w]
    def decode(self, code: torch.Tensor, formerLevel: Union[None, torch.Tensor]):
        q = self._dequantizationHead(self._dequantizer.decode(code))
        if self._sideHead is not None:
            xHat = q + self._sideHead(formerLevel)
        else:
            xHat = q
        return self._restoreHead(xHat)

    def forward(self, q: torch.Tensor, formerLevel: Union[None, torch.Tensor]):
        q = self._dequantizationHead(self._dequantizer(q))
        if self._sideHead is not None:
            xHat = q + self._sideHead(formerLevel)
        else:
            xHat = q
        return self._restoreHead(xHat)


class UMGMQuantizer(BaseQuantizer):
    _components = [
        "latentStageEncoder",
        "quantizationHead",
        "latentHead",
        "dequantizationHead",
        "sideHead",
        "restoreHead"
    ]
    def __init__(self, channel: int, m: int, k: Union[int, List[int]], permutationRate: float, components: Dict[str, Callable[[], nn.Module]]):
        if isinstance(k, int):
            k = [k]
        super().__init__(m, k)
        componentFns = [components[key] for key in self._components]
        latentStageEncoderFn, quantizationHeadFn, latentHeadFn, dequantizationHeadFn, sideHeadFn, restoreHeadFn = componentFns

        encoders = list()
        decoders = list()

        for i, ki in enumerate(k):
            latentStageEncoder = latentStageEncoderFn()
            quantizationHead = quantizationHeadFn()
            latentHead = latentHeadFn() if i < len(k) - 1 else None
            dequantizationHead = dequantizationHeadFn()
            sideHead = sideHeadFn() if i < len(k) - 1 else None
            restoreHead = restoreHeadFn()
            # This magic is called SmallInit, from paper
            # "Transformers without Tears: Improving the Normalization of Self-Attention",
            # https://arxiv.org/pdf/1910.05895.pdf
            # I've tried a series of initilizations, but found this works the best.
            codebook = nn.Parameter(nn.init.normal_(torch.empty(m, ki, channel // m), std=math.sqrt(2 / (5 * channel / m))))
            quantizer = _multiCodebookQuantization(codebook, permutationRate)
            dequantizer = _multiCodebookDeQuantization(codebook)
            encoders.append(_quantizerEncoder(quantizer, dequantizer, latentStageEncoder, quantizationHead, latentHead))
            decoders.append(_quantizerDecoder(dequantizer, dequantizationHead, sideHead, restoreHead))

        self._encoders: nn.ModuleList[_quantizerEncoder] = nn.ModuleList(encoders)
        self._decoders: nn.ModuleList[_quantizerDecoder] = nn.ModuleList(decoders)

    @property
    def Codebooks(self):
        return list(encoder.Codebook for encoder in self._encoders)

    def encode(self, x: torch.Tensor) -> List[torch.Tensor]:
        codes = list()
        i = 0
        for encoder in self._encoders:
            x, code = encoder.encode(x)
            #            [n, m, h, w]
            codes.append(code)
            i += 1
        # lv * [n, m, h, w]
        return codes

    def decode(self, codes: List[torch.Tensor]) -> Union[torch.Tensor, None]:
        formerLevel = None
        i = 0
        for decoder, code in zip(self._decoders[::-1], codes[::-1]):
            formerLevel = decoder.decode(code, formerLevel)
            i += 1
        return formerLevel

    def reAssignCodebook(self) -> torch.Tensor:
        freqs = self.NormalizedFreq
        reassigned: List[torch.Tensor] = list()
        for encoder, freq in zip(self._encoders, freqs):
            # freq: [m, ki]
            reassigned.append(encoder.reAssignCodebook(freq))
        return torch.cat(reassigned).float().mean()

    def syncCodebook(self):
        dist.barrier()
        for encoder in self._encoders:
            encoder.syncCodebook()

    def forward(self, x: torch.Tensor):
        quantizeds = list()
        codes = list()
        oneHots = list()
        logits = list()
        for encoder in self._encoders:
            #          ↓ residual
            quantized, x, code, oneHot, logit = encoder(x)
            # [n, c, h, w]
            quantizeds.append(quantized)
            # [n, m, h, w]
            codes.append(code)
            # [n, m, h, w, k]
            oneHots.append(oneHot)
            # [n, m, h, w, k]
            logits.append(logit)
        formerLevel = None
        for decoder, quantized in zip(self._decoders[::-1], quantizeds[::-1]):
            # ↓ restored
            formerLevel = decoder(quantized, formerLevel)

        # update freq in entropy coder
        self._entropyCoder(oneHots)

        return formerLevel, codes, logits





class NeonQuantizer(VariousMQuantizer):
    def __init__(self, m: List[int], k: List[int]):
        if not isinstance(k, list):
            raise AttributeError

        from mcquic.nn import ResidualBlock, ResidualBlockShuffle, ResidualBlockWithStride
        from mcquic.nn.blocks import AttentionBlock
        from mcquic.nn.convs import conv3x3, conv1x1

        # 16, 13,
        super().__init__(m, k)

        encoders = list()
        decoders = list()

        for i, (ki, mi) in enumerate(zip(k, m)):
            latentStageEncoder = nn.Sequential(
                ResidualBlock(32, 32),
                AttentionBlock(32),
                ResidualBlockWithStride(32, 32),
                conv1x1(32, 32, bias=False)
            )
            quantizationHead = nn.Identity()
            latentHead = nn.Identity()
            codebook = nn.Parameter(nn.init.normal_(torch.empty(mi, ki, 32 // mi), std=math.sqrt(2 / (5 * 32 / float(mi)))))
            quantizer = _multiCodebookQuantization(codebook, 0.5)
            dequantizer = _multiCodebookDeQuantization(codebook)

            dequantizationHead = nn.Identity()
            sideHead = nn.Identity() if i < len(k) - 1 else None
            restoreHead = nn.Sequential(
                conv1x1(32, 32, bias=False),
                ResidualBlockShuffle(32, 32),
                AttentionBlock(32),
                ResidualBlock(32, 32)
            )

            encoders.append(_quantizerEncoder(quantizer, dequantizer, latentStageEncoder, quantizationHead, latentHead))
            decoders.append(_quantizerDecoder(dequantizer, dequantizationHead, sideHead, restoreHead))


        self._encoders: nn.ModuleList[_quantizerEncoder] = nn.ModuleList(encoders)
        self._decoders: nn.ModuleList[_quantizerDecoder] = nn.ModuleList(decoders)

    @property
    def Codebooks(self):
        return list(encoder.Codebook for encoder in self._encoders)

    def encode(self, x: torch.Tensor) -> List[torch.Tensor]:
        codes = list()
        for encoder in self._encoders:
            x, code = encoder.encode(x)
            #            [n, m, h, w]
            codes.append(code)
        # lv * [n, m, h, w]
        return codes

    def decode(self, codes: List[torch.Tensor]) -> Union[torch.Tensor, None]:
        formerLevel = None
        for decoder, code in zip(self._decoders[::-1], codes[::-1]):
            formerLevel = decoder.decode(code, formerLevel)
        return formerLevel

    def reAssignCodebook(self) -> torch.Tensor:
        freqs = self.NormalizedFreq
        reassigned: List[torch.Tensor] = list()
        for encoder, freq in zip(self._encoders, freqs):
            # freq: [m, ki]
            reassigned.append(encoder.reAssignCodebook(freq))
        return torch.cat(reassigned).float().mean()

    def syncCodebook(self):
        dist.barrier()
        for encoder in self._encoders:
            encoder.syncCodebook()

    def forward(self, x: torch.Tensor):
        quantizeds = list()
        codes = list()
        oneHots = list()
        logits = list()
        for encoder in self._encoders:
            #          ↓ residual
            quantized, x, code, oneHot, logit = encoder(x)
            # [n, c, h, w]
            quantizeds.append(quantized)
            # [n, m, h, w]
            codes.append(code)
            # [n, m, h, w, k]
            oneHots.append(oneHot)
            # [n, m, h, w, k]
            logits.append(logit)
        formerLevel = None
        for decoder, quantized in zip(self._decoders[::-1], quantizeds[::-1]):
            # ↓ restored
            formerLevel = decoder(quantized, formerLevel)

        # update freq in entropy coder
        self._entropyCoder(oneHots)

        return formerLevel, codes, logits



class ResidualBackwardQuantizer(VariousMQuantizer):
    def __init__(self, k: int, size: List[int], denseNorm: bool):
        from mcquic.nn import ResidualBlock, ResidualBlockShuffle, ResidualBlockWithStride
        from mcquic.nn.blocks import AttentionBlock
        from mcquic.nn.convs import conv3x3, conv1x1

        channel = 8
        self.channel = channel

        super().__init__([1] * len(size), [k] * len(size))

        encoders = list()
        backwards = list()
        decoders = list()
        quantizers = list()
        dequantizers = list()

        codebook = nn.Parameter(nn.init.trunc_normal_(torch.empty(1, k, channel), std=math.sqrt(2 / (5 * channel))))

        lastSize = size[0] * 2
        # reverse adding encoder, decoder and quantizer
        for i, thisSize in enumerate(size):
            if thisSize == lastSize // 2:
                latentStageEncoder = nn.Sequential(
                    ResidualBlock(channel, channel * 4, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlockWithStride(channel * 4, channel * 4, 2, 1, denseNorm),
                    conv1x1(channel * 4, channel, bias=False)
                )
                # codebook = nn.Parameter(nn.init.zeros_(torch.empty(mi, ki, channel // mi)))
                # NOTE: quantizer is from large to small, but _freqEMA is from small to large
                quantizer = _multiCodebookQuantization(codebook, self._entropyCoder._freqEMA[-(i+1)])
                dequantizer = _multiCodebookDeQuantization(codebook)

                backward = nn.Sequential(
                    conv1x1(channel, channel * 4, bias=False),
                    ResidualBlockShuffle(channel * 4, channel * 4, 2, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlock(channel * 4, channel, 1, denseNorm)
                ) if i < len(size) - 1 else nn.Identity()

                restoreHead = nn.Sequential(
                    conv1x1(channel, channel * 4, bias=False),
                    ResidualBlockShuffle(channel * 4, channel * 4, 2, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlock(channel * 4, channel, 1, denseNorm)
                )
            elif thisSize == lastSize:
                latentStageEncoder = nn.Sequential(
                    ResidualBlock(channel, channel * 4, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlock(channel * 4, channel * 4, 1, denseNorm),
                    conv1x1(channel * 4, channel, bias=False)
                )
                # codebook = nn.Parameter(nn.init.zeros_(torch.empty(mi, ki, channel // mi)))
                # NOTE: quantizer is from large to small, but _freqEMA is from small to large
                quantizer = _multiCodebookQuantization(codebook, self._entropyCoder._freqEMA[-(i+1)])
                dequantizer = _multiCodebookDeQuantization(codebook)

                backward = nn.Sequential(
                    conv1x1(channel, channel * 4, bias=False),
                    ResidualBlock(channel * 4, channel * 4, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlock(channel * 4, channel, 1, denseNorm)
                ) if i < len(size) - 1 else nn.Identity()

                restoreHead = nn.Sequential(
                    conv1x1(channel, channel * 4, bias=False),
                    ResidualBlock(channel * 4, channel * 4, 1, denseNorm),
                    AttentionBlock(channel * 4, 1, denseNorm),
                    ResidualBlock(channel * 4, channel, 1, denseNorm)
                )
            else:
                raise ValueError('The given size sequence does not half or equal to from left to right.')


            lastSize = thisSize

            encoders.append(latentStageEncoder)
            backwards.append(backward)
            decoders.append(restoreHead)
            quantizers.append(quantizer)
            dequantizers.append(dequantizer)

        self._encoders: nn.ModuleList = nn.ModuleList(encoders)
        self._decoders: nn.ModuleList = nn.ModuleList(decoders)
        self._backwards: nn.ModuleList = nn.ModuleList(backwards)
        self._quantizers: nn.ModuleList = nn.ModuleList(quantizers)
        self._dequantizers: nn.ModuleList = nn.ModuleList(dequantizers)

    @property
    def Codebooks(self):
        return list(quantizer._codebook for quantizer in self._quantizers)

    def residual_backward(self, code: torch.Tensor, level: int):
        dequantizer, backward = self._dequantizers[-level], self._backwards[-level]
        quantized = dequantizer.decode(code)
        return backward(quantized)

    def encode(self, x: torch.Tensor) -> List[torch.Tensor]:
        codes = list()
        allLatents = list()
        # firstly, get all latents
        for encoder in self._encoders:
            x = encoder(x)
            allLatents.append(x)
        # calculate smallest code, and produce residuals from small to large
        currentLatent = torch.zeros_like(allLatents[-1])
        for quantizer, dequantizer, backward, latent in zip(self._quantizers[::-1], self._dequantizers[::-1], self._backwards[::-1], allLatents[::-1]):
            residual = latent - currentLatent
            code = quantizer.encode(residual)
            quantized = dequantizer.decode(code)
            # [n, m, h, w]
            codes.append(code)
            currentLatent = backward(quantized)
        # lv * [n, m, h, w]
        return codes

    def decode(self, codes: List[torch.Tensor]) -> Union[torch.Tensor, None]:
        formerLevel = None
        for decoder, dequantizer, code in zip(self._decoders[::-1], self._dequantizers[::-1], codes):
            quantized = dequantizer.decode(code)
            if formerLevel is None:
                formerLevel = decoder(quantized)
            else:
                formerLevel = decoder(quantized + formerLevel)
        return formerLevel

    def residual_forward(self, code: torch.Tensor, formerLevel: torch.Tensor, level: int):
        if formerLevel is None and level > 0:
            raise RuntimeError('For reconstruction after level-0, you should provide not None formerLevel as input.')
        if formerLevel is not None and level == 0:
            raise RuntimeError('For reconstruction at level-0, you should provide None formerLevel as input.')
        decoder, dequantizer = self._decoders[-(level+1)], self._dequantizers[-(level+1)]
        quantized = dequantizer.decode(code)
        return decoder(quantized + formerLevel) if formerLevel is not None else decoder(quantized)

    def reAssignCodebook(self) -> torch.Tensor:
        freqs = self.NormalizedFreq
        reassigned: List[torch.Tensor] = list()
        for quantizer, freq in zip(self._quantizers, freqs):
            # freq: [m, ki]
            reassigned.append(quantizer.reAssignCodebook(freq))
        return torch.cat(reassigned).float().mean()

    def syncCodebook(self):
        dist.barrier()
        for quantizer in self._quantizers:
            quantizer.syncCodebook()

    def forward(self, x: torch.Tensor):
        quantizeds = list()
        codes = list()
        oneHots = list()
        logits = list()
        allLatents = list()
        # firstly, get all latents
        for encoder in self._encoders:
            x = encoder(x)
            allLatents.append(x)

        ######################## ENCODING ########################
        # calculate smallest code, and produce residuals from small to large
        currentLatent = torch.zeros_like(allLatents[-1])
        for quantizer, dequantizer, backward, latent in zip(self._quantizers[::-1], self._dequantizers[::-1], self._backwards[::-1], allLatents[::-1]):
            residual = latent - currentLatent
            sample, code, oneHot, logit = quantizer(residual)
            quantized = dequantizer(sample)
            # [n, c, h, w]
            quantizeds.append(quantized)
            # [n, m, h, w]
            codes.append(code)
            # [n, m, h, w, k]
            oneHots.append(oneHot)
            # [n, m, h, w, k]
            logits.append(logit)
            currentLatent = backward(quantized)

        ######################## DECODING ########################
        # From smallest quantized latent, scale 2x, and sum with next quantized latent
        formerLevel = torch.zeros_like(quantizeds[0])
        for decoder, quantized in zip(self._decoders[::-1], quantizeds):
            # ↓ restored
            formerLevel = decoder(formerLevel + quantized)

        # update freq in entropy coder
        self._entropyCoder(oneHots)

        return formerLevel, codes, logits
