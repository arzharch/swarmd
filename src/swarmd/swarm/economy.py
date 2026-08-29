"""Agent economy: selection pressure with a currency.

The problem this solves. "Pick the best agent" needs a definition of best, and
the obvious ones are wrong:

  - Paying on OUTPUT rewards verbosity. An agent that emits more, wins.
  - Paying on SELF-REPORTED success rewards lying, and selection pressure
    finds that faster than it finds competence.
  - Paying on nothing means bad strategies never die and the population never
    improves.

So agents hold an allowance, spend it on work, and are paid only when the
FROZEN CRITERION says they succeeded. The criterion was authored before any of
this and attacked before it was frozen (ADR-009), which is what makes payment
something an agent cannot award itself.

Bankruptcy and cloning are what turn payment into selection. A bankrupt agent
stops working; a profitable one has its configuration copied into a new agent.
Over a session the population drifts toward whatever actually passes.

This layer sits ON TOP of the cost ledger, and the distinction matters:

    CostAccount   real money, real provider calls, a hard USD ceiling
    Economy       internal credits, an allocation mechanism, no money

Conflating them would make an agent's internal balance look like a bill.
Credits are denominated in *estimated tokens* so the two can be reconciled, but
they are not dollars and are never reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swarmd.ledger import CostAccount


class Bankrupt(RuntimeError):
    """An agent tried to spend credits it does not have."""

    def __init__(self, agent_id: str, balance: float, requested: float) -> None:
        super().__init__(
            f"agent {agent_id} bankrupt: balance {balance:.1f}, needed {requested:.1f}"
        )
        self.agent_id = agent_id
        self.balance = balance
        self.requested = requested


@dataclass(slots=True)
class Account:
    """One agent's standing in the market."""

    agent_id: str
    balance: float
    lineage: tuple[str, ...] = ()      # ancestry, for tracing what spread
    generation: int = 0
    spent: float = 0.0
    earned: float = 0.0
    successes: int = 0
    attempts: int = 0
    alive: bool = True
    death_reason: str = ""
    traits: dict[str, Any] = field(default_factory=dict)

    @property
    def profit(self) -> float:
        return self.earned - self.spent

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def efficiency(self) -> float:
        """Credits spent per verified success. Lower is better.

        The metric selection actually runs on. An agent that succeeds often but
        expensively is not obviously better than one that succeeds less often
        for a tenth of the cost -- on a quota-bound system it is usually worse.
        """
        return self.spent / self.successes if self.successes else float("inf")


class Economy:
    """Credit allocation, payment on verified success, bankruptcy, cloning.

    ANATOMY: starting_balance
      Credits each agent begins with, denominated in estimated tokens. Why
      2000: roughly three medium LLM calls, so an agent gets a few genuine
      attempts before it must produce something. Too low and agents die before
      demonstrating anything, which selects for luck; too high and the market
      exerts no pressure at all and this whole module is decoration.

    ANATOMY: success_reward
      Credits paid per verified success. Why 1.5x the starting balance: a
      successful agent must be able to fund MORE work than it consumed, or the
      population monotonically dies and selection never gets to operate. Below
      1.0x, even a perfect agent starves.

    ANATOMY: clone_threshold
      Profit at which an agent's configuration is copied into a new one. Why
      2x starting balance: proof of repeated success rather than one lucky
      task. Cloning on a single win spreads noise through the population.
    """

    def __init__(
        self,
        *,
        starting_balance: float = 2000.0,
        success_reward: float = 3000.0,
        clone_threshold: float = 4000.0,
        account: CostAccount | None = None,
    ) -> None:
        self.starting_balance = starting_balance
        self.success_reward = success_reward
        self.clone_threshold = clone_threshold
        self.account = account
        self._accounts: dict[str, Account] = {}
        self._next_id = 0

    # -- population ---------------------------------------------------------

    def restore(self, agent_id: str, balance: float) -> Account:
        """Rebuild an account that existed before this process did.

        NOT `spawn`. Spawning mints the next id and hands out a fresh starting
        balance, which is exactly wrong on a resume: it would give a population
        that has already spent its allowance a second one, and the ids would
        collide with the agents whose checkpoints are being restored alongside.

        The id counter is advanced past every restored id so that agents
        spawned later in the resumed run cannot be handed a name that is
        already in use.
        """
        account = Account(agent_id=agent_id, balance=balance)
        self._accounts[agent_id] = account
        if agent_id.startswith("a") and agent_id[1:].isdigit():
            self._next_id = max(self._next_id, int(agent_id[1:]))
        return account

    def spawn(
        self,
        *,
        traits: dict[str, Any] | None = None,
        parent: str | None = None,
    ) -> Account:
        self._next_id += 1
        agent_id = f"a{self._next_id:04d}"
        parent_account = self._accounts.get(parent or "")
        account = Account(
            agent_id=agent_id,
            balance=self.starting_balance,
            lineage=(
                (*parent_account.lineage, parent_account.agent_id)
                if parent_account
                else ()
            ),
            generation=(parent_account.generation + 1) if parent_account else 0,
            traits=dict(traits or (parent_account.traits if parent_account else {})),
        )
        self._accounts[agent_id] = account
        return account

    def get(self, agent_id: str) -> Account:
        account = self._accounts.get(agent_id)
        if account is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return account

    def alive(self) -> list[Account]:
        return [a for a in self._accounts.values() if a.alive]

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    # -- transactions -------------------------------------------------------

    def can_afford(self, agent_id: str, cost: float) -> bool:
        account = self.get(agent_id)
        return account.alive and account.balance >= cost

    def spend(self, agent_id: str, cost: float, *, stage: str = "") -> float:
        """Charge an agent for work. Raises Bankrupt rather than going negative.

        Failing loudly matters: an agent permitted to overdraw is an agent
        exempt from the selection pressure this module exists to apply, and the
        exemption would be invisible.
        """
        account = self.get(agent_id)
        if not account.alive:
            raise Bankrupt(agent_id, 0.0, cost)
        if account.balance < cost:
            self.kill(agent_id, reason="bankrupt")
            raise Bankrupt(agent_id, account.balance, cost)
        account.balance -= cost
        account.spent += cost
        if self.account is not None:
            self.account.record(
                "agent_spend",
                agent_id=agent_id,
                stage=stage,
                detail={"credits": cost, "balance": account.balance},
            )
        return account.balance

    def settle(self, agent_id: str, *, verified_success: bool, stage: str = "") -> float:
        """Pay, or do not, based on the frozen criterion's verdict.

        `verified_success` comes from evaluating the frozen criterion. It is
        never an agent's own claim -- that is the entire design.
        """
        account = self.get(agent_id)
        account.attempts += 1
        if verified_success:
            account.successes += 1
            account.balance += self.success_reward
            account.earned += self.success_reward
        if self.account is not None:
            self.account.record(
                "success" if verified_success else "gate",
                agent_id=agent_id,
                stage=stage,
                detail={
                    "verified": verified_success,
                    "balance": account.balance,
                    "efficiency": (
                        None if account.efficiency == float("inf")
                        else round(account.efficiency, 2)
                    ),
                },
            )
        return account.balance

    def kill(self, agent_id: str, *, reason: str) -> Account:
        account = self.get(agent_id)
        if account.alive:
            account.alive = False
            account.death_reason = reason
            account.balance = 0.0
        return account

    # -- selection ----------------------------------------------------------

    def clone_candidates(self) -> list[Account]:
        """Agents that have proven themselves enough to be copied."""
        return [
            a for a in self.alive()
            if a.profit >= self.clone_threshold and a.successes >= 2
        ]

    def reproduce(self) -> list[Account]:
        """Clone profitable agents. Returns the new accounts.

        The parent pays for the child out of its own balance. Free reproduction
        would let a single lucky agent flood the population at no cost, which is
        drift rather than selection.
        """
        offspring = []
        for parent in self.clone_candidates():
            if parent.balance < self.starting_balance:
                continue
            parent.balance -= self.starting_balance
            offspring.append(self.spawn(parent=parent.agent_id))
        return offspring

    def reap(self) -> list[Account]:
        """Kill agents that cannot afford to do anything."""
        dead = []
        for account in self.alive():
            if account.balance <= 0:
                self.kill(account.agent_id, reason="bankrupt")
                dead.append(account)
        return dead

    # -- reporting ----------------------------------------------------------

    def report(self) -> dict[str, Any]:
        accounts = self.all()
        alive = self.alive()
        productive = [a for a in accounts if a.successes]
        return {
            "population": len(accounts),
            "alive": len(alive),
            "generations": max((a.generation for a in accounts), default=0),
            "total_spent": round(sum(a.spent for a in accounts), 1),
            "total_earned": round(sum(a.earned for a in accounts), 1),
            "successes": sum(a.successes for a in accounts),
            "attempts": sum(a.attempts for a in accounts),
            "bankruptcies": sum(
                1 for a in accounts if a.death_reason == "bankrupt"
            ),
            "contained": sum(
                1 for a in accounts if a.death_reason.startswith("contained")
            ),
            "mean_efficiency": (
                round(sum(a.efficiency for a in productive) / len(productive), 1)
                if productive else None
            ),
        }

    def leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        """Ranked by efficiency, not by raw successes.

        On a quota-bound system, credits spent per success is the number that
        decides whether a strategy is worth spreading.
        """
        ranked = sorted(
            (a for a in self.all() if a.attempts),
            key=lambda a: (a.efficiency, -a.successes),
        )
        return [
            {
                "agent_id": a.agent_id,
                "generation": a.generation,
                "successes": a.successes,
                "attempts": a.attempts,
                "success_rate": round(a.success_rate, 3),
                "efficiency": (
                    None if a.efficiency == float("inf") else round(a.efficiency, 1)
                ),
                "alive": a.alive,
            }
            for a in ranked[:limit]
        ]


def estimate_cost(prompt: str, max_tokens: int) -> float:
    """Credits a call is expected to consume.

    Deliberately an ESTIMATE made before the call: an agent must be refused
    when it cannot afford the work, and charging only afterwards means it has
    already spent quota it did not have. Rough, and rough in the safe direction
    -- roughly four characters per token, plus the full output allowance.
    """
    return len(prompt) / 4.0 + max_tokens
