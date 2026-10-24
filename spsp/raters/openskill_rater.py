"""
OpenSkill raters via the `openskill` library.

Two flavours:
- OpenSkillPLRater: PlackettLuce trained on full tournament standings only.
- OpenSkillBTRater: BradleyTerryFull trained on pairwise match outcomes.
"""
from openskill.models import BradleyTerryFull, PlackettLuce

from .base import Rater


class OpenSkillPLRater(Rater):
    name = "OpenSkill(PL,stand)"
    trains_on = "standings"

    def __init__(self):
        self.model = PlackettLuce()
        self.ratings: dict[int, object] = {}

    def _get(self, p: int):
        if p not in self.ratings:
            self.ratings[p] = self.model.rating()
        return self.ratings[p]

    def update_tournament(self, standings, matches, timestamp=None):
        # Deduplicate by user_id while preserving placement order.
        seen = set()
        teams = []
        ranks = []
        uids = []
        for place, uid in standings:
            if uid in seen:
                continue
            seen.add(uid)
            teams.append([self._get(uid)])
            ranks.append(place)
            uids.append(uid)
        if len(teams) < 2:
            return
        new_ratings = self.model.rate(teams, ranks=ranks)
        for uid, team in zip(uids, new_ratings):
            self.ratings[uid] = team[0]

    def update_match(self, winner: int, loser: int, timestamp=None) -> None:
        # PL variant is standings-only; ignore individual matches.
        pass

    def predict(self, p1: int, p2: int) -> float:
        r1 = self._get(p1)
        r2 = self._get(p2)
        probs = self.model.predict_win([[r1], [r2]])
        return float(probs[0])

    def rating(self, player: int) -> float:
        return self._get(player).ordinal()

    def has_player(self, player: int) -> bool:
        return player in self.ratings


class OpenSkillBTRater(Rater):
    name = "OpenSkill(BT,match)"
    trains_on = "matches"

    def __init__(self):
        self.model = BradleyTerryFull()
        self.ratings: dict[int, object] = {}

    def _get(self, p: int):
        if p not in self.ratings:
            self.ratings[p] = self.model.rating()
        return self.ratings[p]

    def update_match(self, winner: int, loser: int, timestamp=None) -> None:
        w = self._get(winner)
        l = self._get(loser)
        new_ratings = self.model.rate([[w], [l]], ranks=[1, 2])
        self.ratings[winner] = new_ratings[0][0]
        self.ratings[loser] = new_ratings[1][0]

    def predict(self, p1: int, p2: int) -> float:
        r1 = self._get(p1)
        r2 = self._get(p2)
        probs = self.model.predict_win([[r1], [r2]])
        return float(probs[0])

    def rating(self, player: int) -> float:
        return self._get(player).ordinal()

    def has_player(self, player: int) -> bool:
        return player in self.ratings
