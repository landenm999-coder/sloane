"""Scheduled work: the five daily briefs, the nightly rebuild, and the governor.

The jobs themselves are thin. Anything a job needs to be *right* about -- which
things collide, whether it is allowed to speak -- is computed in code here and
handed to the model as FACTS, not left for it to notice.
"""
