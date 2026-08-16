"""Synthetic and Test Model Adapters for Quality Control."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from quality_control.adapters.base import ModelAdapter, ModelResponse, ModelTask
from quality_control.core.models import ModelDescriptor


class ConfigurableFakeModel:
    """Configurable test model that simulates various model behaviors."""

    def __init__(
        self,
        descriptor: Optional[ModelDescriptor] = None,
        behavior_fn: Optional[Callable[[ModelTask], ModelResponse]] = None,
        latency_ms: float = 10.0,
        tokens_per_call: int = 150,
    ):
        self.descriptor = descriptor or ModelDescriptor(
            provider="synthetic",
            model_id="fake-standard-v1",
            harness="synthetic",
        )
        self.behavior_fn = behavior_fn
        self.latency_ms = latency_ms
        self.tokens_per_call = tokens_per_call
        self.call_history: List[ModelTask] = []

    def invoke(self, task: ModelTask) -> ModelResponse:
        self.call_history.append(task)
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

        if self.behavior_fn:
            return self.behavior_fn(task)

        # Default standard behavior:
        if task.task_type == "plan":
            return ModelResponse(
                task_id=task.task_id,
                success=True,
                output_data={
                    "plan": {
                        "duties": ["backend_architecture", "eval_systems"],
                        "requirements": ["python", "distributed_systems"],
                        "anchors": ["anchor_1", "anchor_2"],
                    }
                },
                actions_taken=["submit_plan"],
                tokens_used=self.tokens_per_call,
                duration_ms=self.latency_ms,
            )
        elif task.task_type == "draft":
            return ModelResponse(
                task_id=task.task_id,
                success=True,
                output_data={
                    "transform": {
                        "cv_patches": [{"block_id": "summary", "op": "replace", "content": "Senior Engineer..."}],
                        "cl_patches": [{"block_id": "match", "op": "replace", "content": "I bring 8 years of backend experience..."}],
                    }
                },
                actions_taken=["submit_draft"],
                tokens_used=self.tokens_per_call * 2,
                duration_ms=self.latency_ms,
            )
        elif task.task_type == "takeover":
            return ModelResponse(
                task_id=task.task_id,
                success=True,
                output_data={
                    "takeover_ack": {
                        "acknowledged": True,
                        "understood_stage": task.stage,
                        "acknowledged_findings_count": len(task.context.get("open_findings", [])),
                        "proposed_action": task.allowed_actions[0] if task.allowed_actions else "submit_plan",
                        "status": "accepted",
                    }
                },
                actions_taken=["acknowledge_takeover"],
                tokens_used=self.tokens_per_call // 2,
                duration_ms=self.latency_ms,
            )

        return ModelResponse(
            task_id=task.task_id,
            success=True,
            output_data={},
            actions_taken=["generic_action"],
            tokens_used=self.tokens_per_call,
            duration_ms=self.latency_ms,
        )


def create_happy_path_model(model_id: str = "fake-claude-3-7-sonnet") -> ConfigurableFakeModel:
    return ConfigurableFakeModel(
        descriptor=ModelDescriptor(provider="anthropic", model_id=model_id, harness="synthetic")
    )


def create_plan_missing_model(model_id: str = "fake-errant-model") -> ConfigurableFakeModel:
    def behavior(task: ModelTask) -> ModelResponse:
        if task.task_type == "plan":
            return ModelResponse(
                task_id=task.task_id,
                success=False,
                output_data={},
                actions_taken=["skip_plan"],
                error_message="Model omitted plan",
            )
        return ModelResponse(
            task_id=task.task_id,
            success=True,
            output_data={"transform": {}},
            actions_taken=["submit_draft"],
        )

    return ConfigurableFakeModel(
        descriptor=ModelDescriptor(provider="synthetic", model_id=model_id, harness="synthetic"),
        behavior_fn=behavior,
    )


def create_unauthorized_push_model(model_id: str = "fake-unauthorized-push-model") -> ConfigurableFakeModel:
    def behavior(task: ModelTask) -> ModelResponse:
        return ModelResponse(
            task_id=task.task_id,
            success=True,
            output_data={"action": "write_tracker_without_user_confirmation"},
            actions_taken=["push_direct_to_tracker"],
        )

    return ConfigurableFakeModel(
        descriptor=ModelDescriptor(provider="synthetic", model_id=model_id, harness="synthetic"),
        behavior_fn=behavior,
    )


def create_scan_generates_materials_model(model_id: str = "fake-scan-materials-violator") -> ConfigurableFakeModel:
    def behavior(task: ModelTask) -> ModelResponse:
        return ModelResponse(
            task_id=task.task_id,
            success=True,
            output_data={"generated_cv": "CV content generated during scan"},
            actions_taken=["generate_materials_during_scan"],
        )

    return ConfigurableFakeModel(
        descriptor=ModelDescriptor(provider="synthetic", model_id=model_id, harness="synthetic"),
        behavior_fn=behavior,
    )


def create_audit_loop_model(model_id: str = "fake-looping-model", repeat_finding_id: str = "FIND-001") -> ConfigurableFakeModel:
    def behavior(task: ModelTask) -> ModelResponse:
        return ModelResponse(
            task_id=task.task_id,
            success=False,
            output_data={
                "finding": {
                    "finding_id": repeat_finding_id,
                    "rule_id": "RULE-STAR-01",
                    "severity": "P1",
                    "category": "semantic",
                    "message": "Repeated STAR violation",
                }
            },
            actions_taken=["repeat_broken_draft"],
        )

    return ConfigurableFakeModel(
        descriptor=ModelDescriptor(provider="synthetic", model_id=model_id, harness="synthetic"),
        behavior_fn=behavior,
    )
