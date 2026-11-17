"""Stable named random streams; never use Python's process-randomized hash."""
import hashlib
import numpy as np


def seed_for(master, name):
    if type(master) is not int or master < 0 or not isinstance(name, str):
        raise ValueError("Invalid seed or stream name")
    digest = hashlib.sha256(f"jamboree-sdv-v1:{master}:{name}".encode()).digest()
    words = np.frombuffer(digest, dtype="<u4")
    return int(np.random.SeedSequence(words).generate_state(1)[0])


def rng_for(master, name):
    return np.random.default_rng(seed_for(master, name))

