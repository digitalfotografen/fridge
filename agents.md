# Agent for this project
The project's long-term goal is described in the README.md file.
This project is a Raspberry Pi-based controller for a game fridge.
The fridge has a cooler, a heater, and ventilation fan that are controlled via relays over a I2C bus.
The fridge is controlled by a Raspberry Pi 4 with a custom interface board.
The interface board has a SHT30 sensor and a MCP23017 GPIO.

The Raspberry Pi is accessed via SSH, this is configured in the Pycharm project.
The SSH user and password will be given in the prompts.
The remote project folder is at /home/[user]/fridge
Configuration, data, and logs are stored in /home/[user]/fridge_data.

The application must be able to run without network access or user interaction.

Development is done in steps, and the human developer will be the one to decide which steps to take.
Don't change the code too much, and don't make changes that are not related to the task at hand.
Keep the codebase clean and maintainable.

For each step,
- Create a plane
- Implement tests for the step.
- Implement the step.
- Test the step.
- Update user and developer documentation.

The human developer will handle commits, pull-requests, and merges.

This application is designed to be run on a Raspberry Pi 4 with a custom interface board.
The human developer will supply instructions for how to reach the Raspberry Pi via SSH.
Please ask if the human developer forget to include this in task. This should not be saved or pushed to github


## Python and Environment
- **Python Version**: Use Python 3.13+. Raspberry Pi:s current version is 3.13, so ensure this version is supported.
- **Environment Management**: Use `uv` to manage Python versions and run the application (e.g., `uv run`).
- **Coding Standards**: Follow [PEP 8](https://peps.python.org/pep-0008/).
- **Naming Conventions**: Name variables, classes, and methods clearly to minimize the need for comments.
- **Type Hints**: Use type hints extensively to assist other developers.
- **Test driven development**: Write tests before implementing features to ensure code quality. Keep tests up-to-date with the code.
- **Linting and Formatting**: Use `ruff` for linting and formatting after each iteration.
- **Doc strings**: Use reStr formatted doc strings for documentation (Like JetBrains).

## Package Management

Use `uv` for dependency management and `pyproject.toml` for package metadata.
- List all dependencies in `pyproject.toml` and use groups for organization.
- Always aske before adding new dependencies.

## Testing
Use TDD - always write tests before writing code.
Don't run the test locally, run all tests on the Raspberry Pi.

- **Test Framework**: Use `pytest`.
- **Async Testing**: Use `pytest-asyncio` and `asyncmock`.
- **Mocking**: Use `pytest-mock` and `freezegun` for time mocking.
- **Quality Control**: Use `pytest-timeout` for slow tests and `pytest-cov` for coverage reporting.
- **Verification**: Ensure all tests pass before submitting a pull request.

## Documentation

Document your agents using Markdown (`.md`) files. Documentation is generated using Sphinx with the following requirements:
- `sphinx`
- `myst-parser`
- `sphinx-autodoc2`
- `furo` theme
