"""Quits the Apple apps in BLOCKED the moment they open, however they were launched (Dock, the
play key, a file, a link, AppleScript). They are part of the sealed system volume, so they can't
be deleted; this kills them instead. Their widgets are separate processes and keep working.

It also registers itself as a (paused, silent) media player. When no app has played anything,
macOS sends Play from the play key or a Bluetooth headset to Music and launches it to handle the
command; some headsets resend Play every few seconds until a player answers, so killing Music
alone would mean a relaunch every few seconds. Answering those commands here means nothing
launches. Any app that actually plays (Spotify, a browser) takes the commands back as usual.

Pass --dry-run to log launches without quitting anything.
"""
from lib.common import DRY_RUN, run_main

# The default list (bundle id -> name). Take out any app you use.
BLOCKED = {
    "com.apple.Music": "Music",
    "com.apple.TV": "TV",
    "com.apple.podcasts": "Podcasts",
    # Tips is also the Help Viewer, so every app's Help menu stops opening too.
    "com.apple.helpviewer": "Tips",
    "com.apple.freeform": "Freeform",
    "com.apple.GenerativePlaygroundApp": "Image Playground",
}
SWEEP_SECONDS = 15  # backstop for a launch notification that never arrives (e.g. across sleep)


def claim_media_commands():
    """Become the player that Play/Pause go to when nothing else is playing."""
    from MediaPlayer import (MPMediaItemPropertyTitle, MPNowPlayingInfoCenter,
                             MPNowPlayingPlaybackStatePaused, MPNowPlayingPlaybackStatePlaying,
                             MPRemoteCommandCenter, MPRemoteCommandHandlerStatusSuccess)

    commands = MPRemoteCommandCenter.sharedCommandCenter()
    for command in (commands.playCommand(), commands.pauseCommand(),
                    commands.togglePlayPauseCommand()):
        command.setEnabled_(True)
        command.addTargetWithHandler_(lambda _event: MPRemoteCommandHandlerStatusSuccess)
    now_playing = MPNowPlayingInfoCenter.defaultCenter()
    now_playing.setNowPlayingInfo_({MPMediaItemPropertyTitle: "Apple Music blocked"})
    # Commands go to the player that last played, so "play" for a moment to become it, then pause.
    now_playing.setPlaybackState_(MPNowPlayingPlaybackStatePlaying)
    now_playing.setPlaybackState_(MPNowPlayingPlaybackStatePaused)


def main(log):
    from AppKit import NSWorkspace, NSWorkspaceDidLaunchApplicationNotification
    from Foundation import NSObject, NSTimer
    from PyObjCTools import AppHelper

    workspace = NSWorkspace.sharedWorkspace()

    def kill(app, how):
        name = BLOCKED[app.bundleIdentifier()]
        log.info("%s opened (pid %s, %s)%s", name, app.processIdentifier(), how,
                 ", dry run" if DRY_RUN else "; quitting it")
        # forceTerminate rather than terminate: Music ignores a polite quit while it is starting up.
        if not DRY_RUN and not app.forceTerminate():
            log.warning("could not quit %s (pid %s)", name, app.processIdentifier())

    def sweep(how):
        for app in workspace.runningApplications():
            if app.bundleIdentifier() in BLOCKED and not app.isTerminated():
                kill(app, how)

    class Observer(NSObject):
        def launched_(self, notification):
            app = notification.userInfo()["NSWorkspaceApplicationKey"]
            if app.bundleIdentifier() in BLOCKED:
                kill(app, "launch")

        def sweep_(self, _timer):
            sweep("sweep")

    observer = Observer.alloc().init()
    workspace.notificationCenter().addObserver_selector_name_object_(
        observer, "launched:", NSWorkspaceDidLaunchApplicationNotification, None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        SWEEP_SECONDS, observer, "sweep:", None, True)

    sweep("already running")
    claim_media_commands()
    log.info("started; blocking %s%s", ", ".join(BLOCKED.values()), " (dry run)" if DRY_RUN else "")
    AppHelper.runConsoleEventLoop()


if __name__ == "__main__":
    run_main("app_blocker", main)
