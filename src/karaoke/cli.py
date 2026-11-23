"""Command line: `karaoke [hub|player|guest|rescan-scales]`.

    karaoke hub            the strong machine: queue, processing and all three UIs (default)
    karaoke player         laptop at the screen: admin and player UI with a local song cache
    karaoke guest          VPS: the public page where everybody can queue songs
    karaoke rescan-scales  detect the key of every downloaded song again

Without a command, ROLE from the environment (or .env) decides, else it is the hub.
"""

import argparse
import os

from dotenv import find_dotenv, load_dotenv

COMMANDS = ('hub', 'player', 'guest', 'rescan-scales')


def main(argv=None):
    # Settings are read when karaoke.config is imported, so .env goes first.
    load_dotenv(find_dotenv(usecwd=True))

    parser = argparse.ArgumentParser(prog='karaoke', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', choices=COMMANDS, default=os.getenv('ROLE') or 'hub')
    command = parser.parse_args(argv).command

    # The player and guest node never import the audio stack, so they run without torch & co.
    if command == 'player':
        from karaoke.player import server
    elif command == 'guest':
        from karaoke.guest import server
    elif command == 'rescan-scales':
        from karaoke.hub import song
        return song.rescan_scales()
    else:
        from karaoke.hub import server
    server.start()
