"""
State Rules Engine - Reactive business logic for computed state.

The Rules Engine provides a declarative way to define business logic
that reacts to state changes and computes derived/output state.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Set, Callable, Any, Dict
from enum import Enum, auto

from ..store import Store, StateSlice
from ..app_state import AppState
from ..actions import Action, ActionSource

logger = logging.getLogger(__name__)


class RulePriority(Enum):
    """Rule execution priority."""
    HIGH = 0      # Execute first (sensor processing)
    NORMAL = 50   # Standard priority
    LOW = 100     # Execute last (aggregations)


@dataclass
class RuleResult:
    """Result of rule evaluation."""
    rule_name: str
    triggered: bool
    actions_dispatched: int = 0
    error: Optional[str] = None


class StateRule(ABC):
    """
    Base class for reactive state rules.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Rule identifier for logging/debugging."""
        pass
    
    @property
    @abstractmethod
    def watches(self) -> Set[StateSlice]:
        """State slices this rule reacts to."""
        pass
    
    @property
    def priority(self) -> RulePriority:
        """Rule execution priority (lower = earlier)."""
        return RulePriority.NORMAL
    
    @property
    def enabled(self) -> bool:
        """Whether this rule is currently enabled."""
        return True
    
    @abstractmethod
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        """Evaluate rule and dispatch any resulting actions."""
        pass


class FunctionalRule(StateRule):
    """
    Functional wrapper for simple rules.
    """
    
    def __init__(
        self,
        name: str,
        watches: Set[StateSlice],
        evaluator: Callable[[Optional[AppState], AppState, Store], None],
        priority: RulePriority = RulePriority.NORMAL
    ):
        self._name = name
        self._watches = watches
        self._evaluator = evaluator
        self._priority = priority
        self._enabled = True
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def watches(self) -> Set[StateSlice]:
        return self._watches
    
    @property
    def priority(self) -> RulePriority:
        return self._priority
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this rule."""
        self._enabled = enabled
    
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        self._evaluator(old_state, new_state, store)


@dataclass
class RulesEngineStats:
    """Statistics for the rules engine."""
    evaluations: int = 0
    actions_triggered: int = 0
    errors: int = 0


class RulesEngine:
    """
    Manages and executes state rules.
    """
    
    def __init__(self, store: Store, max_cascades: int = 10):
        """
        Initialize rules engine.
        """
        self._store = store
        self._rules: List[StateRule] = []
        self._prev_state: Optional[AppState] = None
        self._max_cascades = max_cascades
        self._cascade_depth = 0
        
        # Statistics
        self._stats = RulesEngineStats()
        
        # Debug mode
        self._debug = False
        
        # Subscribe to all state changes
        store.subscribe(StateSlice.ALL, self._on_state_change)
    
    @property
    def stats(self) -> RulesEngineStats:
        """Get engine statistics."""
        return self._stats
    
    def set_debug(self, enabled: bool) -> None:
        """Enable/disable debug logging."""
        self._debug = enabled
    
    def register(self, rule: StateRule) -> None:
        """
        Register a rule with the engine.
        """
        self._rules.append(rule)
        # Sort by priority
        self._rules.sort(key=lambda r: r.priority.value)
        logger.info(f"Registered rule: {rule.name} (priority={rule.priority.name})")
    
    def unregister(self, rule_name: str) -> bool:
        """
        Unregister a rule by name.
        """
        for i, rule in enumerate(self._rules):
            if rule.name == rule_name:
                del self._rules[i]
                logger.info(f"Unregistered rule: {rule_name}")
                return True
        return False
    
    def get_rule(self, rule_name: str) -> Optional[StateRule]:
        """Get a rule by name."""
        for rule in self._rules:
            if rule.name == rule_name:
                return rule
        return None
    
    @property
    def rules(self) -> List[StateRule]:
        """Get all registered rules."""
        return self._rules.copy()
    
    def evaluate_all(self, force: bool = False) -> List[RuleResult]:
        """
        Manually evaluate all rules.
        """
        state = self._store.state
        results = []
        
        for rule in self._rules:
            if not rule.enabled:
                continue
            
            result = self._evaluate_rule(rule, self._prev_state, state)
            results.append(result)
        
        if force:
            self._prev_state = state
        
        return results
    
    def _on_state_change(self, state: AppState) -> None:
        """Handle state change from store."""
        if self._cascade_depth >= self._max_cascades:
            logger.warning(f"Maximum cascade depth ({self._max_cascades}) reached, skipping rule evaluation")
            return
        
        if not self._rules:
            self._prev_state = state
            return
        
        # Detect which slices changed
        changed_slices = self._detect_changes(self._prev_state, state)
        
        if not changed_slices:
            self._prev_state = state
            return
        
        # Increment cascade depth
        self._cascade_depth += 1
        
        try:
            # Evaluate applicable rules
            for rule in self._rules:
                if not rule.enabled:
                    continue
                
                # Check if rule watches any changed slices
                if rule.watches & changed_slices:
                    result = self._evaluate_rule(rule, self._prev_state, state)
                    
                    if self._debug and result.triggered:
                        logger.debug(f"Rule '{rule.name}' triggered, dispatched {result.actions_dispatched} actions")
        finally:
            self._cascade_depth -= 1
            self._prev_state = state
    
    def _evaluate_rule(
        self, 
        rule: StateRule, 
        old_state: Optional[AppState], 
        new_state: AppState
    ) -> RuleResult:
        """Evaluate a single rule."""
        self._stats.evaluations += 1
        
        result = RuleResult(
            rule_name=rule.name,
            triggered=False
        )
        
        try:
            # Track dispatches during this evaluation
            dispatch_count_before = getattr(self._store, '_dispatch_count', 0)
            
            rule.evaluate(old_state, new_state, self._store)
            
            dispatch_count_after = getattr(self._store, '_dispatch_count', 0)
            actions_dispatched = dispatch_count_after - dispatch_count_before
            
            result.triggered = True
            result.actions_dispatched = actions_dispatched
            
            if actions_dispatched > 0:
                self._stats.actions_triggered += actions_dispatched
            
        except Exception as e:
            result.error = str(e)
            self._stats.errors += 1
            logger.error(f"Rule '{rule.name}' error: {e}")
        
        return result
    
    def _detect_changes(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState
    ) -> Set[StateSlice]:
        """Detect which state slices changed."""
        if old_state is None:
            # First state - consider all slices changed
            return {
                StateSlice.AUDIO,
                StateSlice.CLIMATE,
                StateSlice.VEHICLE,
                StateSlice.ENERGY,
                StateSlice.CONNECTION,
                StateSlice.DISPLAY,
                StateSlice.VFD_SATELLITE,
            }
        
        changed: Set[StateSlice] = set()
        
        if old_state.audio != new_state.audio:
            changed.add(StateSlice.AUDIO)
        if old_state.climate != new_state.climate:
            changed.add(StateSlice.CLIMATE)
        if old_state.vehicle != new_state.vehicle:
            changed.add(StateSlice.VEHICLE)
        if old_state.energy != new_state.energy:
            changed.add(StateSlice.ENERGY)
        if old_state.connection != new_state.connection:
            changed.add(StateSlice.CONNECTION)
        if old_state.debug != new_state.debug:
            changed.add(StateSlice.DEBUG)
        if old_state.input != new_state.input:
            changed.add(StateSlice.INPUT)
        if old_state.display != new_state.display:
            changed.add(StateSlice.DISPLAY)
        if old_state.vfd_satellite != new_state.vfd_satellite:
            changed.add(StateSlice.VFD_SATELLITE)
        
        return changed


def create_computed_rule(
    name: str,
    watches: Set[StateSlice],
    compute: Callable[[AppState], Any],
    action_factory: Callable[[Any], Action],
    get_current: Callable[[AppState], Any],
    priority: RulePriority = RulePriority.NORMAL
) -> FunctionalRule:
    """
    Create a rule that computes a value and dispatches an action if changed.
    """
    def evaluator(old_state: Optional[AppState], new_state: AppState, store: Store) -> None:
        computed_value = compute(new_state)
        current_value = get_current(new_state)
        
        if computed_value != current_value:
            action = action_factory(computed_value)
            store.dispatch(action)
    
    return FunctionalRule(
        name=name,
        watches=watches,
        evaluator=evaluator,
        priority=priority
    )
