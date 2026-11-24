"""Read private drawing test coordinates from local environment variables."""
import json
import os


def stamp_settings():
    return json.loads(os.environ['VECTOR_TEST_STAMP'])
