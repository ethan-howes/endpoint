"""Ride orchestration (ENDPOINT.md section 7).

Sits between the frontend and S1/S2/S3, and is the only component that sees more
than one service's response -- which makes it the place where partial failure
has to be handled rather than raised.
"""
