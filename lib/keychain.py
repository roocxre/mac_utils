"""Secrets in the login Keychain, through `security`. Values are JSON, stored base64-encoded so
quotes and spaces never reach the line `security -i` parses, and the secret never shows in a
process listing (it goes in on stdin). Name services with lib.identity.keychain_service("<name>"),
so they never collide with another install's."""
import base64
import json
import subprocess


def get(service, account):
    done = subprocess.run(["security", "find-generic-password", "-a", account, "-s", service, "-w"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    try:
        return json.loads(base64.b64decode(done.stdout.strip()))
    except ValueError:
        return None


def put(service, account, value):
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    done = subprocess.run(["security", "-i"], input=f'add-generic-password -U -a {account} -s {service} -w "{encoded}"\n',
                          capture_output=True, text=True)
    if done.returncode != 0 or "error" in done.stderr.lower():
        raise OSError(f"could not save to the Keychain: {done.stderr.strip()[:200]}")


def delete(service, account):
    subprocess.run(["security", "delete-generic-password", "-a", account, "-s", service], capture_output=True)
