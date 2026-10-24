"""Common interface for rating systems."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Rater(ABC):
    """Online rating system.

    Training unit: Tournament. Each tournament has ``standings`` (list of
    (placement, user_id)) and ``matches`` (list of Match). Raters choose
    how to consume them via ``update_tournament``.

    - Match-based raters (Elo, Glicko, TrueSkill, OpenSkill-BT, BT-MLE)
      iterate over ``matches`` and call ``update_match`` internally.
    - Standings-based raters (OpenSkill-PL) ignore ``matches`` and call
      ``rate`` once per tournament using the full ranking.
    """

    name: str = "base"
    trains_on: str = "matches"  # or "standings"

    def update_tournament(
        self,
        standings: list[tuple[int, int]],
        matches: list,
        timestamp: int | None = None,
    ) -> None:
        """Default: consume the matches sequentially."""
        for m in matches:
            self.update_match(m.winner_id, m.loser_id, timestamp)

    @abstractmethod
    def update_match(self, winner: int, loser: int, timestamp: int | None = None) -> None:
        ...

    @abstractmethod
    def rating(self, player: int) -> float:
        """A scalar summary of player strength (higher = stronger)."""
        ...

    def has_player(self, player: int) -> bool:
        return False

    # Optional: pairwise win probability, used by some diagnostics.
    def predict(self, p1: int, p2: int) -> float:  # noqa: D401
        raise NotImplementedError
