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
    demo.py       simulated drive for screenshots / desk demo

Importing this package does not import pygame; ``page``/``render`` do.
"""
