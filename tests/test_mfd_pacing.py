"""Frame pacing of the MFD UI: the pacer, the scene's redraw hints, the main
screen's chrome cache and the framebuffer's direct-blit path.

    SDL_VIDEODRIVER=dummy .venv-fe/bin/python -m pytest tests/test_mfd_pacing.py
"""

import mmap
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["SDL_FBDEV"] = "/nonexistent/fb-for-tests"

pygame = pytest.importorskip("pygame")

from cyberpunk_computer.ui.scene import demo  # noqa: E402
from cyberpunk_computer.ui.scene.pacing import SLACK, FramePacer  # noqa: E402
from cyberpunk_computer.ui.scene.tuning import Tuning  # noqa: E402

SIZE = (480, 240)


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    pygame.init()
    pygame.display.set_mode(SIZE)
    from cyberpunk_computer.ui.fonts import fonts
    fonts._fonts.clear()
    yield


@pytest.fixture(autouse=True)
def _no_settings_writes(monkeypatch):
    import cyberpunk_computer.persistence as persistence
    import cyberpunk_computer.ui.screens.main_screen as ms
    for mod in (persistence, ms):
        if hasattr(mod, "save_settings"):
            monkeypatch.setattr(mod, "save_settings", lambda *a, **k: None)
    monkeypatch.setattr(persistence.SettingsManager, "save", lambda self, *a, **k: True)


# ── tunables ────────────────────────────────────────────────────────────────

def test_frame_rates_are_tunables():
    t = Tuning()
    assert {"fps_scene", "fps_touch", "fps_pulse", "fps_idle", "touch_hold_s"} <= set(t.names())
    t = Tuning({"fps_scene": 6, "fps_idle": 0.5, "fps_touch": "fast"})   # wrong type ignored
    assert t.fps_scene == 6.0 and t.fps_idle == 0.5 and t.fps_touch == Tuning.fps_touch


# ── pacer ───────────────────────────────────────────────────────────────────

def _pacer(**kw):
    return FramePacer(Tuning(kw), clock=lambda: 0.0)


def test_nothing_changed_renders_only_at_the_idle_rate():
    p = _pacer(fps_idle=1.0)
    assert p.due(0.0)                      # the first frame
    p.rendered(0.0)
    assert not any(p.due(t / 100.0) for t in range(1, 100))
    assert p.due(1.0)


def test_changes_render_promptly_but_at_most_at_the_touch_rate():
    p = _pacer(fps_touch=20.0)
    p.rendered(0.0)
    assert not p.due(0.01, changed=True)                 # too soon after the last frame
    assert p.due(SLACK * 0.05, changed=True)
    assert p.due(0.05, animating=True)
    assert not p.due(0.05)                               # no reason


def test_pulse_renders_at_the_pulse_rate():
    p = _pacer(fps_pulse=5.0, fps_touch=20.0)
    p.rendered(0.0)
    assert not p.due(0.1, pulsing=True)
    assert p.due(0.2, pulsing=True)


def test_input_holds_the_touch_rate_and_the_loop_rate():
    p = _pacer(fps_touch=20.0, fps_scene=10.0, touch_hold_s=2.0)
    p.rendered(0.0)
    assert p.loop_fps(0.0) == 10.0
    p.note_input(0.5)
    assert p.loop_fps(1.0) == 20.0 and p.interacting(2.4)
    p.rendered(0.5)
    assert p.due(0.55)                                   # interacting: every touch-rate slot
    assert not p.interacting(2.6) and p.loop_fps(2.6) == 10.0
    assert p.loop_fps(2.6, pulsing=True) == 20.0


def test_loop_runs_at_fps_scene_only_while_something_moves():
    p = _pacer(fps_touch=20.0, fps_scene=20.0, fps_loop=10.0)
    assert p.loop_fps(0.0) == 10.0                       # parked: as before (10 passes/s)
    assert p.loop_fps(0.0, animating=True) == 20.0       # driving: every scene frame on time
    assert p.loop_fps(0.0, pulsing=True) == 20.0
    q = _pacer(fps_touch=20.0, fps_scene=5.0, fps_loop=10.0)
    assert q.loop_fps(0.0) == 5.0                        # never above fps_scene


def test_frames_at_the_loop_rate_are_due():
    """A 20 fps scene with a 20 Hz loop: each pass is due when the frame is
    stamped with the decision time (core/app.py), even if rendering it took
    20 ms; stamped at the end of the render every other pass was skipped."""
    p = _pacer(fps_touch=20.0, fps_scene=20.0)
    t = 0.0
    p.rendered(t)
    for _ in range(20):
        t += 0.05
        assert p.due(t, animating=True)
        p.rendered(t)
    late = _pacer(fps_touch=20.0, fps_scene=20.0)
    late.rendered(0.02)                                  # end of a 20 ms render
    assert not late.due(0.05, animating=True)


def test_main_screen_moving_follows_the_scene():
    sim = demo.Sim("live")
    sim.run_to(1.0)                                      # parked, steady
    assert not sim.screen.moving()
    sim.run_to(20.0)                                     # driving
    assert sim.screen.moving()


def test_scene_frames_between_events_are_new():
    """20 fps with road events at 10 Hz: every scene redraw differs from the
    previous one (lanes / lead ease towards the newest event, the floor
    scrolls), half of them fall between two events."""
    from cyberpunk_computer.ui.scene.inputs import SCENE_EVENT
    sim = demo.Sim("live", mods={"tuning": {"fps_scene": 20.0}})
    sim.run_to(17.0)
    page = sim.screen.scene_page
    surf = pygame.Surface(SIZE)
    sim.frame(surf)
    t, seq, last_ev = sim.t, 1000, -1.0
    prev, same, between, redraws, ev = None, 0, 0, 0, 0
    while t < 21.0:
        t += 0.05
        sim.t = t
        if t - last_ev >= 0.1 - 1e-9:
            sc = demo.scene_at(t, seq + 1)
            if sc is not None:
                seq, last_ev, ev = seq + 1, t, ev + 1
                sim.feed.push(SCENE_EVENT, sc)
        sim.screen.update(0.05)
        if page.scene.due(page.director, page.inputs, t):
            sim.frame(surf)
            raw = page.scene.surface.get_buffer().raw
            redraws += 1
            same += raw == prev
            between += ev == 0
            prev, ev = raw, 0
    assert redraws >= 75 and same == 0 and between >= 30, (redraws, same, between)


def test_poke_forces_the_next_frame():
    p = _pacer()
    p.rendered(0.0)
    assert not p.due(0.3)
    p.poke()
    assert p.due(0.3)
    p.rendered(0.3)
    assert not p.due(0.4)


# ── scene hints ─────────────────────────────────────────────────────────────

def test_scene_due_predicts_render():
    sim = demo.Sim("live")
    sim.run_to(20.0)                       # driving: animating
    surf = pygame.Surface(SIZE)
    sim.frame(surf)
    page = sim.screen.scene_page
    sc = page.scene
    for _ in range(40):
        sim.step(0.03)
        due = sc.due(page.director, page.inputs, sim.t)
        before = sc.redraws
        sim.frame(surf)
        assert (sc.redraws > before) == due


def test_scene_redraw_rate_follows_fps_scene():
    for fps in (10.0, 5.0):
        sim = demo.Sim("live", mods={"tuning": {"fps_scene": fps}})
        sim.run_to(20.0)
        surf = pygame.Surface(SIZE)
        sim.frame(surf)
        sc = sim.screen.scene_page.scene
        n0 = sc.redraws
        for _ in range(100):               # 3 s at a 33 Hz loop
            sim.step(0.03)
            sim.frame(surf)
        rate = (sc.redraws - n0) / 3.0
        assert fps * 0.8 <= rate <= fps * 1.3, (fps, rate)


def test_parked_scene_page_asks_for_no_frames():
    sim = demo.Sim("live")
    sim.run_to(1.0)
    surf = pygame.Surface(SIZE)
    for _ in range(3):
        sim.step(0.05)
        sim.frame(surf)
    page = sim.screen.scene_page
    sim.screen.update(0.05)
    sim.t += 0.05
    assert page.needs_frame() is False
    page.history._flush()                  # the power history moved on
    assert page.needs_frame() is True


# ── main screen ─────────────────────────────────────────────────────────────

def test_main_screen_reports_changes_and_pulses():
    sim = demo.Sim("live")
    sim.run_to(1.0)
    surf = pygame.Surface(SIZE)
    sim.frame(surf)
    sim.screen.update(0.05)
    sim.frame(surf)
    changed, animating, pulsing = sim.screen.frame_state()
    assert not changed and not pulsing
    sim.store.push(demo.state_at(1.2))     # a store snapshot
    assert sim.screen.frame_state()[0]
    sim.frame(surf)
    assert not sim.screen.frame_state()[0]
    from cyberpunk_computer.input.manager import InputEvent
    sim.screen.handle_input(InputEvent.ROTATE_RIGHT)   # focus shows: panels pulse
    assert sim.screen.frame_state()[2]


def _advance(sim, t_end: float, dt: float = 0.05) -> None:
    """Like Sim.run_to, with store snapshots at 1 Hz (the backend's rate)
    instead of every step; roadsense events at 5 Hz."""
    from cyberpunk_computer.ui.scene.inputs import SCENE_EVENT
    while sim.t < t_end - 1e-9:
        prev = sim.t
        sim.t += dt
        if int(sim.t) != int(prev) or prev == 0.0:
            sim.store.push(demo.state_at(sim.t))
        if sim.t - sim._last_scene_t >= 0.2:
            sc = demo.scene_at(sim.t, sim._seq + 1)
            if sc is not None:
                sim._seq += 1
                sim._last_scene_t = sim.t
                sim.feed.push(SCENE_EVENT, sc)
        sim.screen.update(dt)


def _frames(partial: bool):
    """Frames of the demo timeline drawn with the chrome cache (and, with
    ``partial``, centre-only re-draws on a persistent surface)."""
    sim = demo.Sim("live")
    surf = pygame.Surface(SIZE)
    out = []
    t = 0.0
    while t < 24.0:
        t += 0.1
        _advance(sim, t)
        if not partial:
            surf.fill((0, 0, 0))
        sim.screen.partial_ok = partial and t > 0.15
        sim.screen.render(surf)
        out.append((round(t, 1), pygame.image.tobytes(surf, "RGB"), sim.screen.present_rect))
    return out


def _reference_frames():
    sim = demo.Sim("live")
    surf = pygame.Surface(SIZE)
    out = []
    t = 0.0
    while t < 24.0:
        t += 0.1
        _advance(sim, t)
        sim.screen.invalidate()            # every frame drawn from scratch
        surf.fill((0, 0, 0))
        sim.screen.render(surf)
        out.append(pygame.image.tobytes(surf, "RGB"))
    return out


def test_chrome_cache_and_centre_redraws_are_pixel_identical():
    ref = _reference_frames()
    full = _frames(partial=False)
    part = _frames(partial=True)
    assert len(ref) == len(full) == len(part)
    for r, (t, f, _), (_, p, _) in zip(ref, full, part):
        assert f == r, "chrome cache differs at t=%s" % t
        assert p == r, "centre re-draw differs at t=%s" % t
    # the partial path was taken (most frames reuse the chrome)
    centre = [rect for _, _, rect in part if rect is not None]
    assert len(centre) > len(part) // 2
    assert all(rect.width == 240 and rect.height == 240 for rect in centre)


# ── framebuffer direct blit ─────────────────────────────────────────────────

class _MemFB:
    """FramebufferOutput over anonymous memory (no /dev/fb0)."""

    def __new__(cls, w=480, h=240, stride=None):
        from cyberpunk_computer.core.framebuffer import FramebufferOutput
        fb = FramebufferOutput("/nonexistent")
        fb.width, fb.height, fb.bpp = w, h, 32
        fb.line_length = stride or w * 4
        fb.mmap = mmap.mmap(-1, fb.line_length * h)
        fb._initialized = True
        return fb


def _picture():
    s = pygame.Surface(SIZE)
    s.fill((10, 200, 30))
    pygame.draw.line(s, (255, 0, 255), (0, 0), (479, 239))
    pygame.draw.rect(s, (1, 2, 3), (5, 5, 50, 50))
    pygame.draw.circle(s, (250, 128, 7), (300, 120), 40)
    return s


def test_direct_blit_matches_the_converting_path():
    s = _picture()
    fast = _MemFB()
    assert fast.blit_surface(s)
    assert fast._fb_surface is not None
    legacy = _MemFB(stride=480 * 4)
    legacy._fb_surface_tried = True        # force the converting path
    assert legacy.blit_surface(s)
    assert bytes(fast.mmap[:]) == bytes(legacy.mmap[:])
    assert bytes(fast.mmap[3:16:4]) == b"\xff" * 4       # alpha opaque

    # a rect update writes only the rect
    s2 = _picture()
    s2.fill((0, 0, 255), (120, 0, 240, 240))
    centre = pygame.Rect(120, 0, 240, 240)
    fast.blit_surface(s2, centre)
    legacy.blit_surface(s2)
    assert bytes(fast.mmap[:]) == bytes(legacy.mmap[:])
    fast.cleanup()


def test_direct_blit_not_used_with_padded_rows_or_gains(monkeypatch):
    padded = _MemFB(stride=480 * 4 + 64)
    assert padded.blit_surface(_picture())
    assert padded._fb_surface is None
    monkeypatch.setenv("MFD_GAIN_R", "1.2")
    from cyberpunk_computer.core.framebuffer import FramebufferOutput
    eq = FramebufferOutput("/nonexistent")
    eq.width, eq.height, eq.bpp, eq.line_length = 480, 240, 32, 480 * 4
    eq.mmap = mmap.mmap(-1, 480 * 240 * 4)
    eq._initialized = True
    assert eq.blit_surface(_picture())
    assert eq._fb_surface is None
