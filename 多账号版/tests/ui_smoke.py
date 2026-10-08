"""Compatibility notice for the retired Playwright smoke runner.

The old selectors described the former account-card/modal UI. Browser validation
of the redesigned console uses CUA against tests/ui_fixture_server.py. This file
is retained so old command references explain the replacement; it does not launch
any browser, read real configuration/account data, or claim that UI tests passed.
"""
from pathlib import Path


def main():
    fixture = Path(__file__).with_name('ui_fixture_server.py')
    frontend = Path(__file__).with_name('test_frontend.cjs')
    motion = Path(__file__).with_name('test_motion.cjs')
    print('The legacy Playwright UI smoke runner has been retired.')
    print('Run isolated controller regression: node ' + str(frontend))
    print('Run isolated motion regression: node ' + str(motion))
    print('Start the fake localhost backend: python ' + str(fixture) + ' --port 8765')
    print('Verify desktop/mobile interactions with CUA at http://127.0.0.1:8765/.')
    print('Dummy token: ui-fixture-token. All API data is fake; no real account is used.')
    print('This compatibility command does not perform browser verification.')


if __name__ == '__main__':
    main()
