# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Neptune Logger
--------------
"""
__all__ = [
    "NeptuneLogger",
]

import logging
import os
from argparse import Namespace
from functools import reduce
from typing import Any, Dict, Generator, Optional, Set, Union
from weakref import ReferenceType

import torch

from pytorch_lightning import __version__
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from pytorch_lightning.loggers.base import LightningLoggerBase, rank_zero_experiment
from pytorch_lightning.utilities import rank_zero_only
from pytorch_lightning.utilities.imports import _NEPTUNE_AVAILABLE, _NEPTUNE_GREATER_EQUAL_0_9
from pytorch_lightning.utilities.model_summary import ModelSummary

if _NEPTUNE_AVAILABLE and _NEPTUNE_GREATER_EQUAL_0_9:
    try:
        from neptune import new as neptune
        from neptune.new.exceptions import NeptuneLegacyProjectException, NeptuneOfflineModeFetchException
        from neptune.new.run import Run
    except ImportError:
        import neptune
        from neptune.exceptions import NeptuneLegacyProjectException
        from neptune.run import Run
else:
    # needed for test mocks, and function signatures
    neptune, Run = None, None

log = logging.getLogger(__name__)

_INTEGRATION_VERSION_KEY = "source_code/integrations/pytorch-lightning"

# kwargs used in previous NeptuneLogger version, now deprecated
_LEGACY_NEPTUNE_INIT_KWARGS = [
    "project_name",
    "offline_mode",
    "experiment_name",
    "experiment_id",
    "params",
    "properties",
    "upload_source_files",
    "abort_callback",
    "logger",
    "upload_stdout",
    "upload_stderr",
    "send_hardware_metrics",
    "run_monitoring_thread",
    "handle_uncaught_exceptions",
    "git_info",
    "hostname",
    "notebook_id",
    "notebook_path",
]

# kwargs used in legacy NeptuneLogger from neptune-pytorch-lightning package
_LEGACY_NEPTUNE_LOGGER_KWARGS = [
    "base_namespace",
    "close_after_fit",
]


class NeptuneLogger(LightningLoggerBase):
    r"""
    Log using `Neptune <https://neptune.ai>`_.

    Install it with pip:

    .. code-block:: bash

        pip install neptune-client

    or conda:

    .. code-block:: bash

        conda install -c conda-forge neptune-client

    **Quickstart**

    Pass NeptuneLogger instance to the Trainer to log metadata with Neptune:

    .. testcode::

        from pytorch_lightning import Trainer
        from pytorch_lightning.loggers import NeptuneLogger

        neptune_logger = NeptuneLogger(
            api_key="ANONYMOUS",  # replace with your own
            project="common/pytorch-lightning-integration",  # format "<WORKSPACE/PROJECT>"
            tags=["training", "resnet"],  # optional
        )
        trainer = Trainer(max_epochs=10, logger=neptune_logger)

    **How to use NeptuneLogger?**

    Use the logger anywhere in your :class:`~pytorch_lightning.core.lightning.LightningModule` as follows:

    .. code-block:: python

        from neptune.new.types import File
        from pytorch_lightning import LightningModule


        class LitModel(LightningModule):
            def training_step(self, batch, batch_idx):
                # log metrics
                acc = ...
                self.log("train/loss", loss)

            def any_lightning_module_function_or_hook(self):
                # log images
                img = ...
                self.logger.experiment["train/misclassified_images"].log(File.as_image(img))

                # generic recipe
                metadata = ...
                self.logger.experiment["your/metadata/structure"].log(metadata)

    Check `Logging metadata docs <https://docs.neptune.ai/you-should-know/logging-metadata>`_
    for more info about how to log various types of metadata (scores, files, images, interactive visuals, CSVs, etc.).

    **Log after fitting or testing is finished**

    You can log objects after the fitting or testing methods are finished:

    .. code-block:: python

        neptune_logger = NeptuneLogger(project="common/pytorch-lightning-integration")

        trainer = pl.Trainer(logger=neptune_logger)
        model = ...
        datamodule = ...
        trainer.fit(model, datamodule=datamodule)
        trainer.test(model, datamodule=datamodule)

        # Log objects after `fit` or `test` methods
        # model summary
        neptune_logger.log_model_summary(model=model, max_depth=-1)

        # generic recipe
        metadata = ...
        neptune_logger.experiment["your/metadata/structure"].log(metadata)

    **Log model checkpoints**

    If you have :class:`~pytorch_lightning.callbacks.ModelCheckpoint` configured,
    Neptune logger automatically logs model checkpoints.
    Model weights will be uploaded to the: "model/checkpoints" namespace in the Neptune Run.
    You can disable this option:

    .. code-block:: python

        neptune_logger = NeptuneLogger(project="common/pytorch-lightning-integration", log_model_checkpoints=False)

    **Pass additional parameters to the Neptune run**

    You can also pass ``neptune_run_kwargs`` to specify the run in the greater detail, like ``tags`` or ``description``:

    .. testcode::

        from pytorch_lightning import Trainer
        from pytorch_lightning.loggers import NeptuneLogger

        neptune_logger = NeptuneLogger(
            project="common/pytorch-lightning-integration",
            name="lightning-run",
            description="mlp quick run with pytorch-lightning",
            tags=["mlp", "quick-run"],
        )
        trainer = Trainer(max_epochs=3, logger=neptune_logger)

    Check `run documentation <https://docs.neptune.ai/essentials/api-reference/run>`_
    for more info about additional run parameters.

    **Details about Neptune run structure**

    Runs can be viewed as nested dictionary-like structures that you can define in your code.
    Thanks to this you can easily organize your metadata in a way that is most convenient for you.

    The hierarchical structure that you apply to your metadata will be reflected later in the UI.

    You can organize this way any type of metadata - images, parameters, metrics, model checkpoint, CSV files, etc.

    See Also:
        - Read about
          `what object you can log to Neptune <https://docs.neptune.ai/you-should-know/what-can-you-log-and-display>`_.
        - Check `example run <https://app.neptune.ai/o/common/org/pytorch-lightning-integration/e/PTL-1/all>`_
          with multiple types of metadata logged.
        - For more detailed info check
          `user guide <https://docs.neptune.ai/integrations-and-supported-tools/model-training/pytorch-lightning>`_.

    Args:
        api_key: Optional.
            Neptune API token, found on https://neptune.ai upon registration.
            Read: `how to find and set Neptune API token <https://docs.neptune.ai/administration/security-and-privacy/
            how-to-find-and-set-neptune-api-token>`_.
            It is recommended to keep it in the `NEPTUNE_API_TOKEN`
            environment variable and then you can drop ``api_key=None``.
        project: Optional.
            Name of a project in a form of "my_workspace/my_project" for example "tom/mask-rcnn".
            If ``None``, the value of `NEPTUNE_PROJECT` environment variable will be taken.
            You need to create the project in https://neptune.ai first.
        name: Optional. Editable name of the run.
            Run name appears in the "all metadata/sys" section in Neptune UI.
        run: Optional. Default is ``None``. The Neptune ``Run`` object.
            If specified, this `Run`` will be used for logging, instead of a new Run.
            When run object is passed you can't specify other neptune properties.
        log_model_checkpoints: Optional. Default is ``True``. Log model checkpoint to Neptune.
            Works only if ``ModelCheckpoint`` is passed to the ``Trainer``.
        prefix: Optional. Default is ``"training"``. Root namespace for all metadata logging.
        \**neptune_run_kwargs: Additional arguments like ``tags``, ``description``, ``capture_stdout``, etc.
            used when run is created.

    Raises:
        ImportError:
            If required Neptune package in version >=0.9 is not installed on the device.
        TypeError:
            If configured project has not been migrated to new structure yet.
        ValueError:
            If argument passed to the logger's constructor is incorrect.
    """

    LOGGER_JOIN_CHAR = "/"
    PARAMETERS_KEY = "hyperparams"
    ARTIFACTS_KEY = "artifacts"

    def __init__(
        self,
        *,  # force users to call `NeptuneLogger` initializer with `kwargs`
        api_key: Optional[str] = None,
        project: Optional[str] = None,
        name: Optional[str] = None,
        run: Optional["Run"] = None,
        log_model_checkpoints: Optional[bool] = True,
        prefix: str = "training",
        **neptune_run_kwargs,
    ):

        # verify if user passed proper init arguments
        self._verify_input_arguments(api_key, project, name, run, neptune_run_kwargs)

        super().__init__()
        self._log_model_checkpoints = log_model_checkpoints
        self._prefix = prefix

        self._run_instance = self._init_run_instance(api_key, project, name, run, neptune_run_kwargs)

        self._run_short_id = self.run._short_id  # skipcq: PYL-W0212
        try:
            self.run.wait()
            self._run_name = self._run_instance["sys/name"].fetch()
        except NeptuneOfflineModeFetchException:
            self._run_name = "offline-name"

    @staticmethod
    def _init_run_instance(api_key, project, name, run, neptune_run_kwargs) -> Run:
        if run is not None:
            run_instance = run
        else:
            try:
                run_instance = neptune.init(
                    project=project,
                    api_token=api_key,
                    name=name,
                    **neptune_run_kwargs,
                )
            except NeptuneLegacyProjectException as e:
                raise TypeError(
                    f"""Project {project} has not been migrated to the new structure.
                    You can still integrate it with the Neptune logger using legacy Python API
                    available as part of neptune-contrib package:
                      - https://docs-legacy.neptune.ai/integrations/pytorch_lightning.html\n
                    """
                ) from e

        # make sure that we've log integration version for both newly created and outside `Run` instances
        run_instance[_INTEGRATION_VERSION_KEY] = __version__

        return run_instance

    def _construct_path_with_prefix(self, *keys) -> str:
        """Return sequence of keys joined by `LOGGER_JOIN_CHAR`, started with
        `_prefix` if defined."""
        if self._prefix:
            return self.LOGGER_JOIN_CHAR.join([self._prefix, *keys])
        return self.LOGGER_JOIN_CHAR.join(keys)

    @staticmethod
    def _verify_input_arguments(
        api_key: Optional[str],
        project: Optional[str],
        name: Optional[str],
        run: Optional["Run"],
        neptune_run_kwargs: dict,
    ):

        # check if user used legacy kwargs expected in `NeptuneLegacyLogger`
        used_legacy_kwargs = [
            legacy_kwarg for legacy_kwarg in neptune_run_kwargs if legacy_kwarg in _LEGACY_NEPTUNE_INIT_KWARGS
        ]
        if used_legacy_kwargs:
            raise ValueError(
                f"Following kwargs are deprecated: {used_legacy_kwargs}.\n"
                "If you are looking for the Neptune logger using legacy Python API,"
                " it's still available as part of neptune-contrib package:\n"
                "  - https://docs-legacy.neptune.ai/integrations/pytorch_lightning.html\n"
                "The NeptuneLogger was re-written to use the neptune.new Python API\n"
                "  - https://neptune.ai/blog/neptune-new\n"
                "  - https://docs.neptune.ai/integrations-and-supported-tools/model-training/pytorch-lightning\n"
                "You should use arguments accepted by either NeptuneLogger.init() or neptune.init()"
            )

        # check if user used legacy kwargs expected in `NeptuneLogger` from neptune-pytorch-lightning package
        used_legacy_neptune_kwargs = [
            legacy_kwarg for legacy_kwarg in neptune_run_kwargs if legacy_kwarg in _LEGACY_NEPTUNE_LOGGER_KWARGS
        ]
        if used_legacy_neptune_kwargs:
            raise ValueError(
                f"Following kwargs are deprecated: {used_legacy_neptune_kwargs}.\n"
                "If you are looking for the Neptune logger using legacy Python API,"
                " it's still available as part of neptune-contrib package:\n"
                "  - https://docs-legacy.neptune.ai/integrations/pytorch_lightning.html\n"
                "The NeptuneLogger was re-written to use the neptune.new Python API\n"
                "  - https://neptune.ai/blog/neptune-new\n"
                "  - https://docs.neptune.ai/integrations-and-supported-tools/model-training/pytorch-lightning\n"
                "You should use arguments accepted by either NeptuneLogger.init() or neptune.init()"
            )

        # check if user passed new client `Run` object
        if run is not None and not isinstance(run, Run):
            raise ValueError(
                "Run parameter expected to be of type `neptune.new.Run`.\n"
                "If you are looking for the Neptune logger using legacy Python API,"
                " it's still available as part of neptune-contrib package:\n"
                "  - https://docs-legacy.neptune.ai/integrations/pytorch_lightning.html\n"
                "The NeptuneLogger was re-written to use the neptune.new Python API\n"
                "  - https://neptune.ai/blog/neptune-new\n"
                "  - https://docs.neptune.ai/integrations-and-supported-tools/model-training/pytorch-lightning\n"
            )

        # check if user passed redundant neptune.init arguments when passed run
        any_neptune_init_arg_passed = any(arg is not None for arg in [api_key, project, name]) or neptune_run_kwargs
        if run is not None and any_neptune_init_arg_passed:
            raise ValueError(
                "When an already initialized run object is provided"
                " you can't provide other neptune.init() parameters.\n"
            )

    def __getstate__(self):
        state = self.__dict__.copy()
        # Run instance can't be pickled
        state["_run_instance"] = None
        return state

    @property
    @rank_zero_experiment
    def experiment(self) -> Run:
        r"""
        Actual Neptune run object. Allows you to use neptune logging features in your
        :class:`~pytorch_lightning.core.lightning.LightningModule`.

        Example::

            class LitModel(LightningModule):
                def training_step(self, batch, batch_idx):
                    # log metrics
                    acc = ...
                    self.logger.experiment["train/acc"].log(acc)

                    # log images
                    img = ...
                    self.logger.experiment["train/misclassified_images"].log(File.as_image(img))
        """
        return self.run

    @property
    def run(self) -> Run:
        return self._run_instance

    @rank_zero_only
    def log_hyperparams(self, params: Union[Dict[str, Any], Namespace]) -> None:  # skipcq: PYL-W0221
        r"""
        Log hyper-parameters to the run.

        Hyperparams will be logged under the "<prefix>/hyperparams" namespace.

        Note:

            You can also log parameters by directly using the logger instance:
            ``neptune_logger.experiment["model/hyper-parameters"] = params_dict``.

            In this way you can keep hierarchical structure of the parameters.

        Args:
            params: `dict`.
                Python dictionary structure with parameters.

        Example::

            from pytorch_lightning.loggers import NeptuneLogger

            PARAMS = {
                "batch_size": 64,
                "lr": 0.07,
                "decay_factor": 0.97
            }

            neptune_logger = NeptuneLogger(
                api_key="ANONYMOUS",
                project="common/pytorch-lightning-integration"
            )

            neptune_logger.log_hyperparams(PARAMS)
        """
        params = self._convert_params(params)
        params = self._sanitize_callable_params(params)

        parameters_key = self.PARAMETERS_KEY
        parameters_key = self._construct_path_with_prefix(parameters_key)

        self.run[parameters_key] = params

    @rank_zero_only
    def log_metrics(self, metrics: Dict[str, Union[torch.Tensor, float]], step: Optional[int] = None) -> None:
        """Log metrics (numeric values) in Neptune runs.

        Args:
            metrics: Dictionary with metric names as keys and measured quantities as values.
            step: Step number at which the metrics should be recorded, currently ignored.
        """
        if rank_zero_only.rank != 0:
            raise ValueError("run tried to log from global_rank != 0")

        metrics = self._add_prefix(metrics)

        for key, val in metrics.items():
            # `step` is ignored because Neptune expects strictly increasing step values which
            # Lighting does not always guarantee.
            self.experiment[key].log(val)

    @rank_zero_only
    def finalize(self, status: str) -> None:
        if status:
            self.experiment[self._construct_path_with_prefix("status")] = status

        super().finalize(status)

    @property
    def save_dir(self) -> Optional[str]:
        """Gets the save directory of the experiment which in this case is ``None`` because Neptune does not save
        locally.

        Returns:
            the root directory where experiment logs get saved
        """
        return os.path.join(os.getcwd(), ".neptune")

    def log_model_summary(self, model, max_depth=-1):
        model_str = str(ModelSummary(model=model, max_depth=max_depth))
        self.experiment[self._construct_path_with_prefix("model/summary")] = neptune.types.File.from_content(
            content=model_str, extension="txt"
        )

    def after_save_checkpoint(self, checkpoint_callback: "ReferenceType[ModelCheckpoint]") -> None:
        """
        Automatically log checkpointed model.
        Called after model checkpoint callback saves a new checkpoint.

        Args:
            checkpoint_callback: the model checkpoint callback instance
        """
        if not self._log_model_checkpoints:
            return

        file_names = set()
        checkpoints_namespace = self._construct_path_with_prefix("model/checkpoints")

        # save last model
        if checkpoint_callback.last_model_path:
            model_last_name = self._get_full_model_name(checkpoint_callback.last_model_path, checkpoint_callback)
            file_names.add(model_last_name)
            self.experiment[f"{checkpoints_namespace}/{model_last_name}"].upload(checkpoint_callback.last_model_path)

        # save best k models
        for key in checkpoint_callback.best_k_models.keys():
            model_name = self._get_full_model_name(key, checkpoint_callback)
            file_names.add(model_name)
            self.experiment[f"{checkpoints_namespace}/{model_name}"].upload(key)

        # remove old models logged to experiment if they are not part of best k models at this point
        if self.experiment.exists(checkpoints_namespace):
            exp_structure = self.experiment.get_structure()
            uploaded_model_names = self._get_full_model_names_from_exp_structure(exp_structure, checkpoints_namespace)

            for file_to_drop in list(uploaded_model_names - file_names):
                del self.experiment[f"{checkpoints_namespace}/{file_to_drop}"]

        # log best model path and best model score
        if checkpoint_callback.best_model_path:
            self.experiment[
                self._construct_path_with_prefix("model/best_model_path")
            ] = checkpoint_callback.best_model_path
        if checkpoint_callback.best_model_score:
            self.experiment[self._construct_path_with_prefix("model/best_model_score")] = (
                checkpoint_callback.best_model_score.cpu().detach().numpy()
            )

    @staticmethod
    def _get_full_model_name(model_path: str, checkpoint_callback: "ReferenceType[ModelCheckpoint]") -> str:
        """Returns model name which is string `modle_path` appended to `checkpoint_callback.dirpath`."""
        expected_model_path = f"{checkpoint_callback.dirpath}/"
        if not model_path.startswith(expected_model_path):
            raise ValueError(f"{model_path} was expected to start with {expected_model_path}.")
        return model_path[len(expected_model_path) :]

    @classmethod
    def _get_full_model_names_from_exp_structure(cls, exp_structure: dict, namespace: str) -> Set[str]:
        """Returns all paths to properties which were already logged in `namespace`"""
        structure_keys = namespace.split(cls.LOGGER_JOIN_CHAR)
        uploaded_models_dict = reduce(lambda d, k: d[k], [exp_structure, *structure_keys])
        return set(cls._dict_paths(uploaded_models_dict))

    @classmethod
    def _dict_paths(cls, d: dict, path_in_build: str = None) -> Generator:
        for k, v in d.items():
            path = f"{path_in_build}/{k}" if path_in_build is not None else k
            if not isinstance(v, dict):
                yield path
            else:
                yield from cls._dict_paths(v, path)

    @property
    def name(self) -> str:
        """Return the experiment name or 'offline-name' when exp is run in offline mode."""
        return self._run_name

    @property
    def version(self) -> str:
        """Return the experiment version. It's Neptune Run's short_id"""
        return self._run_short_id

    @staticmethod
    def _raise_deprecated_api_usage(f_name, sample_code):
        raise ValueError(
            f"The function you've used is deprecated.\n"
            f"If you are looking for the Neptune logger using legacy Python API,"
            f" it's still available as part of neptune-contrib package:\n"
            f"  - https://docs-legacy.neptune.ai/integrations/pytorch_lightning.html\n"
            f"The NeptuneLogger was re-written to use the neptune.new Python API\n"
            f"  - https://neptune.ai/blog/neptune-new\n"
            f"  - https://docs.neptune.ai/integrations-and-supported-tools/model-training/pytorch-lightning\n"
            f"Instead of `logger.{f_name}` you can use:\n"
            f"\t{sample_code}"
        )

    @rank_zero_only
    def log_metric(self, *args, **kwargs):
        self._raise_deprecated_api_usage("log_metric", f"logger.run['{self._prefix}/key'].log(42)")

    @rank_zero_only
    def log_text(self, *args, **kwargs):
        self._raise_deprecated_api_usage("log_text", f"logger.run['{self._prefix}/key'].log('text')")

    @rank_zero_only
    def log_image(self, *args, **kwargs):
        self._raise_deprecated_api_usage("log_image", f"logger.run['{self._prefix}/key'].log(File('path_to_image'))")

    @rank_zero_only
    def log_artifact(self, *args, **kwargs):
        self._raise_deprecated_api_usage(
            "log_artifact", f"logger.run['{self._prefix}/{self.ARTIFACTS_KEY}/key'].log('path_to_file')"
        )

    @rank_zero_only
    def set_property(self, *args, **kwargs):
        self._raise_deprecated_api_usage(
            "log_artifact", f"logger.run['{self._prefix}/{self.PARAMETERS_KEY}/key'].log(value)"
        )

    @rank_zero_only
    def append_tags(self, *args, **kwargs):
        self._raise_deprecated_api_usage("append_tags", "logger.run['sys/tags'].add(['foo', 'bar'])")
