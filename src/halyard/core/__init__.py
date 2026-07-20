"""Core domain of the control plane.

Nothing in this package may import from `halyard.channels` or `halyard.agents`.
Core knows about agents and channels only through the protocols they implement
and the vocabulary defined here.
"""
