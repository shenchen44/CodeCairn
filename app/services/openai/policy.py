from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    name: str
    enable_staged_localization: bool = True
    enable_hybrid_retrieval: bool = True
    enable_memory: bool = True
    enable_deep_review: bool = True
    enable_patch_tournament: bool = False
    enable_patch_recovery: bool = False
    enable_standard_review: bool = False
    mutation_deadline_turn: int | None = None
    stall_turn_limit: int = 3
    mutation_reserve_turns: int = 3
    patch_recovery_attempts: int = 0
    localization_min_confidence: float = 0.55

    def to_dict(self) -> dict:
        return asdict(self)


POLICY_PRESETS = {
    "legacy": RuntimePolicy(
        name="legacy",
        enable_staged_localization=False,
        enable_hybrid_retrieval=False,
        enable_memory=False,
        enable_deep_review=False,
    ),
    "retrieval": RuntimePolicy(
        name="retrieval",
        enable_memory=False,
        enable_deep_review=False,
    ),
    "memory": RuntimePolicy(
        name="memory",
        enable_deep_review=False,
    ),
    "full": RuntimePolicy(
        name="full",
        enable_patch_tournament=True,
        enable_patch_recovery=True,
        enable_standard_review=True,
        stall_turn_limit=3,
        mutation_reserve_turns=3,
        patch_recovery_attempts=2,
    ),
}


def get_runtime_policy(name: str | None = None) -> RuntimePolicy:
    normalized = (name or "full").strip().lower()
    try:
        return POLICY_PRESETS[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(POLICY_PRESETS))
        raise ValueError(
            f"unknown_runtime_policy:{normalized}; expected one of {choices}"
        ) from exc
