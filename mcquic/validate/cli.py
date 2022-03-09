import os
import click
import pathlib
import logging


def checkArgs(debug: bool, quiet: bool, path: pathlib.Path, output: pathlib.Path):
    if path.is_dir():
        raise ValueError("Please provide a file path to `path`, not a dir.")
    if output.is_dir() and not output.exists():
        raise ValueError("`output` dir does not exist.")
    if quiet:
        return logging.CRITICAL
    if debug:
        return logging.DEBUG
    return logging.INFO


def main(debug: bool, quiet: bool, path: pathlib.Path, images: pathlib.Path, output: pathlib.Path) -> int:
    loggingLevel = checkArgs(debug, quiet, path, output)

    import shutil, gzip

    import torch
    from vlutils.logger import configLogging

    from mcquic.config import Config
    from mcquic.modules.compressor import Compressor
    from mcquic.train.utils import getRichProgress
    from mcquic.datasets import getValLoader

    from .validator import Validator

    logger = configLogging(None, "root", loggingLevel)

    checkpoint = torch.load(path, "cuda")

    config = Config.deserialize(checkpoint["config"])

    model = Compressor(**config.Model.Params).cuda()

    modelStateDict = {key[len("module._compressor."):]: value for key, value in checkpoint["trainer"]["_model"].items()}

    model.load_state_dict(modelStateDict) # type: ignore

    validator = Validator(config, "cuda")

    valLoader = getValLoader(images, False, logger)

    progress = getRichProgress()

    with progress:
        _, summary = validator.validate(checkpoint["trainer"]["_epoch"], model, valLoader, progress)
        logger.info(summary)
        _, speedSummary = validator.speed(checkpoint["trainer"]["_epoch"], model, progress)
        logger.info(speedSummary)

    if output.is_dir():
        modelName = "_".join([f"{key}_{value}" for key, value in config.Model.params.items()])
        modelName = modelName.replace(", ", "_").replace("[", "").replace("]", "")
        output = output.joinpath(f"{modelName}_{config.Training.Target.lower()}.mcquic")

    torch.save({
        "model": model.state_dict(),
        "config": config.serialize()
    }, output)

    logger.info(f"Saved at `{output}`.")

    return 0


@click.command()
@click.option("-D", "--debug", is_flag=True, help="Set logging level to DEBUG to print verbose messages.")
@click.option("-q", "--quiet", is_flag=True, help="Silence all messages, this option has higher priority to `-D/--debug`.")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=pathlib.Path), required=True, nargs=1)
@click.argument("images", type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=pathlib.Path), required=True, nargs=1)
@click.argument("output", type=click.Path(exists=False, dir_okay=True, resolve_path=True, path_type=pathlib.Path), required=True, nargs=1)
def entryPoint(debug, quiet, path, images, output):
    """Validate a trained model from `path` by images from `images` dir, and publish a final state_dict to `output` path.

Args:

    path (str): Saved checkpoint path.

    images (str): Validation images folder.

    output (str): File path or dir to publish this model.
    """
    main(debug, quiet, path, images, output)