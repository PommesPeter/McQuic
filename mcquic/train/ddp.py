from shutil import copy2
from typing import Tuple, Union
import os
import functools

import torch
from torch import nn
import torch.distributed as dist
from vlutils.config import summary

from mcquic import Config, Consts
from mcquic.modules.compressor import BaseCompressor, Compressor
from mcquic.loss import CompressionLossBig
from mcquic.datasets import getTrainLoader, getValLoader
from mcquic.utils.registry import OptimizerRegistry, LrSchedulerRegistry

from .utils import getSaver, initializeBaseConfigs
from .trainer import getTrainer


def registerForTrain():
    import mcquic.train.lrSchedulers
    import apex
    OptimizerRegistry.register("Adam")(torch.optim.Adam)
    OptimizerRegistry.register("Lamb")(functools.partial(apex.optimizers.FusedLAMB, set_grad_none=True))

    LrSchedulerRegistry.register("ReduceLROnPlateau")(torch.optim.lr_scheduler.ReduceLROnPlateau)
    LrSchedulerRegistry.register("Exponential")(torch.optim.lr_scheduler.ExponentialLR)
    LrSchedulerRegistry.register("MultiStep")(torch.optim.lr_scheduler.MultiStepLR)
    LrSchedulerRegistry.register("OneCycle")(torch.optim.lr_scheduler.OneCycleLR) # type: ignore


def modelFn(modelParams, lossTarget) -> Tuple[BaseCompressor, nn.Module]:
    compressor = Compressor(**modelParams)
    criterion = CompressionLossBig(lossTarget)

    return compressor, criterion


def ddpSpawnTraining(rank: int, worldSize: int, port: str, config: Config, saveDir: str, resume: Union[str, None], debug: bool):
    registerForTrain()


    # load ckpt before create trainer, in case it moved to other place.
    if resume is not None and os.path.exists(resume) and resume.endswith("ckpt"):
        if rank == 0:
            tmpFile = copy2(resume, os.path.join(Consts.TempDir, "resume.ckpt"), follow_symlinks=False)
        else:
            tmpFile = os.path.join(Consts.TempDir, "resume.ckpt")
    else:
        tmpFile = None


    saver = getSaver(saveDir, saveName="saved.ckpt", loggerName=Consts.Name, loggingLevel="DEBUG" if debug else "INFO", config=config.serialize(), reserve=False, disable=rank != 0)

    saver.info("Here is the whole config during this run: \r\n%s", summary(config.serialize()))

    saver.debug("Creating the world...")

    initializeBaseConfigs(port, rank, worldSize, logger=saver)
    saver.debug("Base configs initialized.")

    dist.barrier()

    optimizerFn = OptimizerRegistry.get(config.Optim.Type, logger=saver)
    schdrFn = LrSchedulerRegistry.get(config.Schdr.type, logger=saver)

    trainer = getTrainer(rank, config, lambda: modelFn(config.Model.Params, config.Training.Target), optimizerFn, schdrFn, saver)

    if tmpFile is not None:
        saver.info("Found ckpt to resume at %s", resume)
        trainer.restoreStates(tmpFile)

    trainLoader, trainSampler = getTrainLoader(rank, worldSize, config.Training.TrainSet, config.Training.BatchSize, logger=saver)
    valLoader = getValLoader(config.Training.ValSet, disable=rank != 0, logger=saver)
    saver.debug("Train and validation datasets mounted.")

    trainer.train(trainLoader, trainSampler, valLoader)

    saver.debug(summary(config.serialize()))
    saver.info("Bye.")
