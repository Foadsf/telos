"""Telos benchmarks: the same agent, model and user prompts, with and without Telos in the repository.

`bench.scenarios` builds the repositories, `bench.score` judges a finished run from ground truth,
and `bench.ab` is the command line. The user interface is driven by an external runner (see
`bench/README.md`); nothing here talks to a model.
"""
