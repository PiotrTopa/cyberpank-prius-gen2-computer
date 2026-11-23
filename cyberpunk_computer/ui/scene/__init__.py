"""
Main-screen scene: the car when parked, the road when driving.

Design and data contract: docs/MFD_MAIN_SCREEN.md.

    inputs.py     AppState -> SceneInputs (all field names; no pygame)
    director.py   view / vision state machine and animation (no pygame)
    model.py      NHW20 vector model, camera (no pygame)
    car.py        car renderer
    render.py     scene renderer (cached surface)
    powerbox.py   power strip under the scene
    page.py       ScenePage: what MainScreen talks to
    radar.py      radar view: 360-degree ATC-style scope of the tracked road users (button on the road view)
    radar_button.py  the button's geometry (no pygame: MainScreen registers its touch zone at start-up)
    demo.py       simulated drive for screenshots / desk demo
    radar_demo.py radar-view situations: screenshots, bench, desk demo

Importing this package does not import pygame; ``page``/``render`` do.
"""
