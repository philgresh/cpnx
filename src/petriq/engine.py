import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from petriq.places import PacedResourcePlace, Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import Transition
from petriq.visualization import snapshot, to_dot


class PetriNet:
    def __init__(self, max_workers: int = 4, error_place: str = "failed"):
        self.max_workers = max_workers
        self.error_place = error_place
        self.places: dict[str, Place] = {}
        self.transitions: dict[str, Transition] = {}
        self._lock = threading.Lock()
        self._running_count = 0
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Callbacks
        self.on_transition_fired: Callable[[str, float], None] | None = None
        self.on_token_deposited: Callable[[str, Token], None] | None = None
        self.on_error: Callable[[str, Exception, Token | None], None] | None = None

        # Add the default error place
        self.add_place(Place(error_place))

    def add_place(self, place: Place) -> None:
        with self._lock:
            self.places[place.name] = place

    def add_transition(self, transition: Transition) -> None:
        with self._lock:
            self.transitions[transition.name] = transition

    def deposit(self, place_name: str, token: Token) -> None:
        with self._lock:
            self._deposit_under_lock(place_name, token)

    def _deposit_under_lock(self, place_name: str, token: Token) -> None:
        if place_name not in self.places:
            self.places[place_name] = Place(place_name)
        place = self.places[place_name]
        place.deposit(token)

        if self.on_token_deposited:
            try:
                self.on_token_deposited(place_name, token)
            except Exception:
                pass

    def _is_transition_enabled(self, transition: Transition) -> bool:
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if not place:
                return False

            if not place.can_retrieve(arc.count):
                return False

            if arc.settle_secs > 0.0:
                with place._lock:
                    last_dep = place.last_deposit_time
                if time.monotonic() - last_dep < arc.settle_secs:
                    return False

        if transition.guard is not None:
            try:
                if not transition.guard():
                    return False
            except Exception:
                return False

        return True

    def step(self) -> bool:
        with self._lock:
            enabled_transitions = []
            for t in self.transitions.values():
                if self._is_transition_enabled(t):
                    enabled_transitions.append(t)

            if not enabled_transitions:
                return False

            # Sort by priority (lower priority = fires first)
            enabled_transitions.sort(key=lambda t: t.priority)
            selected = enabled_transitions[0]

            # Consume tokens
            consumed_tokens = []
            token_sources = []
            for arc in selected.inputs:
                place = self.places[arc.place]
                if arc.consume_all:
                    tokens = place.retrieve_all()
                else:
                    tokens = place.retrieve(arc.count)
                consumed_tokens.extend(tokens)
                for t in tokens:
                    token_sources.append((arc.place, t))

            self._running_count += 1

            # Dispatch action on thread pool
            self._executor.submit(self._execute_transition, selected, consumed_tokens, token_sources)
            return True

    def _execute_transition(
        self, transition: Transition, consumed_tokens: list[Token], token_sources: list[tuple[str, Token]]
    ) -> None:
        start_time = time.monotonic()
        success = False
        output_tokens = []
        error = None

        try:
            output_tokens = transition.action(consumed_tokens)
            success = True
        except Exception as e:
            error = e
        finally:
            with self._lock:
                self._running_count -= 1
                duration = time.monotonic() - start_time

                if success:
                    # Distribute output tokens to target places
                    resource_tokens = [t for t in consumed_tokens if t.is_resource]
                    res_deque = deque(resource_tokens)
                    out_deque = deque(output_tokens)

                    for arc in transition.outputs:
                        place_name = arc.place
                        if place_name not in self.places:
                            self.places[place_name] = Place(place_name)
                        place = self.places[place_name]

                        is_res_place = isinstance(place, (ResourcePlace, PacedResourcePlace))
                        for _ in range(arc.count):
                            if is_res_place:
                                if res_deque:
                                    t = res_deque.popleft()
                                else:
                                    t = Token(is_resource=True)
                                self._deposit_under_lock(place_name, t)
                            else:
                                if out_deque:
                                    t = out_deque.popleft()
                                else:
                                    t = Token()
                                self._deposit_under_lock(place_name, t)

                    if self.on_transition_fired:
                        try:
                            self.on_transition_fired(transition.name, duration)
                        except Exception:
                            pass
                else:
                    # Failure case: return resource tokens and send data tokens to error place
                    for src_name, t in token_sources:
                        if t.is_resource:
                            self._deposit_under_lock(src_name, t)

                    data_tokens = [t for _, t in token_sources if not t.is_resource]
                    for dt in data_tokens:
                        self._deposit_under_lock(self.error_place, dt)

                    if self.on_error:
                        if data_tokens:
                            for dt in data_tokens:
                                try:
                                    self.on_error(transition.name, error, dt)
                                except Exception:
                                    pass
                        else:
                            try:
                                self.on_error(transition.name, error, None)
                            except Exception:
                                pass

    def run(self, deadline: float) -> None:
        while not self.is_quiescent():
            if time.monotonic() > deadline:
                break
            if not self.step():
                time.sleep(0.005)

    def _is_transition_potentially_enabled(self, transition: Transition) -> bool:
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if not place:
                return False

            if isinstance(place, PacedResourcePlace):
                with place._lock:
                    if len(place._tokens) < arc.count:
                        return False
            else:
                if not place.can_retrieve(arc.count):
                    return False

        if transition.guard is not None:
            try:
                if not transition.guard():
                    return False
            except Exception:
                return False

        return True

    def is_quiescent(self) -> bool:
        with self._lock:
            if self._running_count > 0:
                return False
            for t in self.transitions.values():
                if self._is_transition_potentially_enabled(t):
                    return False
            return True

    def snapshot(self) -> dict:
        return snapshot(self)

    def to_dot(self) -> str:
        return to_dot(self)

    def __del__(self):
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
