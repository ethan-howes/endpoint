"""Weather and cover ranking for the conditions a rider will actually meet
(ENDPOINT.md section 6).

Split into fetching and deciding. ``weather.py`` talks to a forecast API;
``cover.py`` talks to Overpass; ``scoring.py`` is pure arithmetic shared by the
rain and sun paths; and this module sequences them into the one call the
orchestrator makes.
"""
