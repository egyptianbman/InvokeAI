from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Tuple

import torch
from diffusers import UNet2DConditionModel

from invokeai.backend.stable_diffusion.extensions.base import ExtensionBase
from invokeai.backend.util.devices import TorchDevice

if TYPE_CHECKING:
    from invokeai.app.invocations.model import LoRAField
    from invokeai.app.services.shared.invocation_context import InvocationContext
    from invokeai.backend.lora import LoRAModelRaw


class LoRAPatcherExt(ExtensionBase):
    def __init__(
        self,
        node_context: InvocationContext,
        loras: List[LoRAField],
        prefix: str,
    ):
        super().__init__()
        self._loras = loras
        self._prefix = prefix
        self._node_context = node_context

    @contextmanager
    def patch_unet(self, unet: UNet2DConditionModel, cached_weights: Optional[Dict[str, torch.Tensor]] = None):
        def _lora_loader() -> Iterator[Tuple[LoRAModelRaw, float]]:
            for lora in self._loras:
                lora_info = self._node_context.models.load(lora.lora)
                lora_model = lora_info.model
                yield (lora_model, lora.weight)
                del lora_info
            return

        yield self._patch_model(
            model=unet,
            prefix=self._prefix,
            loras=_lora_loader(),
            cached_weights=cached_weights,
        )

    @classmethod
    @contextmanager
    def static_patch_model(
        cls,
        model: torch.nn.Module,
        prefix: str,
        loras: Iterator[Tuple[LoRAModelRaw, float]],
        cached_weights: Optional[Dict[str, torch.Tensor]] = None,
    ):
        modified_cached_weights, modified_weights = cls._patch_model(
            model=model,
            prefix=prefix,
            loras=loras,
            cached_weights=cached_weights,
        )
        try:
            yield

        finally:
            with torch.no_grad():
                for param_key in modified_cached_weights:
                    model.get_parameter(param_key).copy_(cached_weights[param_key])
                for param_key, weight in modified_weights.items():
                    model.get_parameter(param_key).copy_(weight)

    @classmethod
    def _patch_model(
        cls,
        model: UNet2DConditionModel,
        prefix: str,
        loras: Iterator[Tuple[LoRAModelRaw, float]],
        cached_weights: Optional[Dict[str, torch.Tensor]] = None,
    ):
        """
        Apply one or more LoRAs to a model.
        :param model: The model to patch.
        :param loras: An iterator that returns the LoRA to patch in and its patch weight.
        :param prefix: A string prefix that precedes keys used in the LoRAs weight layers.
        :cached_weights: Read-only copy of the model's state dict in CPU, for unpatching purposes.
        """
        if cached_weights is None:
            cached_weights = {}

        modified_weights = {}
        modified_cached_weights = set()
        with torch.no_grad():
            for lora, lora_weight in loras:
                # assert lora.device.type == "cpu"
                for layer_key, layer in lora.layers.items():
                    if not layer_key.startswith(prefix):
                        continue

                    # TODO(ryand): A non-negligible amount of time is currently spent resolving LoRA keys. This
                    # should be improved in the following ways:
                    # 1. The key mapping could be more-efficiently pre-computed. This would save time every time a
                    #    LoRA model is applied.
                    # 2. From an API perspective, there's no reason that the `ModelPatcher` should be aware of the
                    #    intricacies of Stable Diffusion key resolution. It should just expect the input LoRA
                    #    weights to have valid keys.
                    assert isinstance(model, torch.nn.Module)
                    module_key, module = cls._resolve_lora_key(model, layer_key, prefix)

                    # All of the LoRA weight calculations will be done on the same device as the module weight.
                    # (Performance will be best if this is a CUDA device.)
                    device = module.weight.device
                    dtype = module.weight.dtype

                    layer_scale = layer.alpha / layer.rank if (layer.alpha and layer.rank) else 1.0

                    # We intentionally move to the target device first, then cast. Experimentally, this was found to
                    # be significantly faster for 16-bit CPU tensors being moved to a CUDA device than doing the
                    # same thing in a single call to '.to(...)'.
                    layer.to(device=device)
                    layer.to(dtype=torch.float32)

                    # TODO(ryand): Using torch.autocast(...) over explicit casting may offer a speed benefit on CUDA
                    # devices here. Experimentally, it was found to be very slow on CPU. More investigation needed.
                    for param_name, lora_param_weight in layer.get_parameters(module).items():
                        param_key = module_key + "." + param_name
                        module_param = module.get_parameter(param_name)

                        # save original weight
                        if param_key not in modified_cached_weights and param_key not in modified_weights:
                            if param_key in cached_weights:
                                modified_cached_weights.add(param_key)
                            else:
                                modified_weights[param_key] = module_param.detach().to(
                                    device=TorchDevice.CPU_DEVICE, copy=True
                                )

                        if module_param.shape != lora_param_weight.shape:
                            # TODO: debug on lycoris
                            lora_param_weight = lora_param_weight.reshape(module_param.shape)

                        lora_param_weight *= lora_weight * layer_scale
                        module_param += lora_param_weight.to(dtype=dtype)

                    layer.to(device=TorchDevice.CPU_DEVICE)

        return modified_cached_weights, modified_weights

    @staticmethod
    def _resolve_lora_key(model: torch.nn.Module, lora_key: str, prefix: str) -> Tuple[str, torch.nn.Module]:
        assert "." not in lora_key

        if not lora_key.startswith(prefix):
            raise Exception(f"lora_key with invalid prefix: {lora_key}, {prefix}")

        module = model
        module_key = ""
        key_parts = lora_key[len(prefix) :].split("_")

        submodule_name = key_parts.pop(0)

        while len(key_parts) > 0:
            try:
                module = module.get_submodule(submodule_name)
                module_key += "." + submodule_name
                submodule_name = key_parts.pop(0)
            except Exception:
                submodule_name += "_" + key_parts.pop(0)

        module = module.get_submodule(submodule_name)
        module_key = (module_key + "." + submodule_name).lstrip(".")

        return (module_key, module)
