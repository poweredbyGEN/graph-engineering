#!/usr/bin/env python3
"""One scheduled owner for conservative QA Docker and filesystem reclamation."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

AGE = 86400
ROOT = Path('/')


def log(message):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} {message}', flush=True)


def command(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


def related(a, b):
    return a == b or a.startswith(b.rstrip('/') + '/') or b.startswith(a.rstrip('/') + '/')


def inspect_objects(ids, image=False):
    if not ids:
        return []
    args = ['docker'] + (['image'] if image else []) + ['inspect', *ids]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    rows = json.loads(result.stdout)
    if result.returncode:
        errors = result.stderr.strip().splitlines()
        # A vanished object has no running owner; any other refusal leaves ownership unknown.
        if not errors or any(not line.startswith(('Error: No such object:', 'Error: No such image:')) for line in errors):
            raise RuntimeError('DOCKER_INSPECTION_FAILED: ' + result.stderr.strip())
        log('KEEP vanished Docker objects: ' + result.stderr.strip())
    return rows


def activity():
    # Unknown activity never means unused: lsof failure retains every filesystem candidate.
    try:
        output = command(['lsof', '-nP', '-Fpn'], 45)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('OPEN_SCAN_FAILED') from exc
    held = {line[1:] for line in output.splitlines() if line.startswith('n/')}
    building = False
    deadline = time.monotonic() + 20
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        if time.monotonic() > deadline:
            raise RuntimeError('PROCESS_SCAN_TIMEOUT')
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            held.add(str((proc / 'cwd').resolve(strict=True)))
        except (FileNotFoundError, ProcessLookupError):
            continue
        for arg in args:
            value = arg.split('=', 1)[-1]
            if value.startswith('/'):
                held.add(value)
        if args and Path(args[0]).name == 'docker' and ('build' in args or 'buildx' in args):
            building = True
    ids = command(['docker', 'ps', '-aq']).split()
    rows = inspect_objects(ids)
    mounts = {m['Source'] for row in rows if row['State']['Running'] for m in row['Mounts']}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        point = line.split()[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
        if point != '/':
            mounts.add(point)
    return held, mounts, building, rows


def safe(path, held, mounts, now):
    try:
        return safe_existing(path, held, mounts, now)
    except FileNotFoundError:
        return False


def safe_existing(path, held, mounts, now):
    if path.is_symlink() or not path.exists() or path.stat().st_dev != ROOT.stat().st_dev:
        return False
    if now - path.stat().st_mtime < AGE:
        return False
    name = str(path)
    owner = str(path.parent) if path.name in ('node_modules', '.venv', 'venv') else name
    if any(h == owner or h.startswith(owner + '/') for h in held) or any(related(name, m) for m in mounts):
        return False
    # Recheck current activity immediately before removal; a timeout retains the candidate.
    try:
        use = subprocess.run(['fuser', '-s', name], timeout=10)
        if use.returncode != 1:
            return False
        if path.is_dir():
            # Preserve secret material, database files, recent writes and nested mounts.
            found = command(['find', name, '-xdev', '(', '-newermt', f'@{now - AGE}',
                             '-o', '-name', '.env', '-o', '-name', '.env.local',
                             '-o', '-name', '.env.production', '-o', '-name', '.env.staging',
                             '-o', '-name', '.credentials',
                             '-o', '-name', 'PG_VERSION', '-o', '-name', '*.sqlite*',
                             '-o', '-name', '*.db', ')', '-print', '-quit'], 20)
            if found:
                return False
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def candidates():
    # Root-resident caches need their own tiers: Docker's data-root is on another disk.
    for directory in ('/var/cache/woodpecker/package-cache/node-modules',
                      '/var/lib/gen-qa-lanes', '/var/lib/gen-qa-evidence',
                      '/woodpecker-cache', '/root/.npm/_cacache'):
        root = Path(directory)
        if root.is_dir() and not root.is_symlink():
            for artifact in root.iterdir():
                if artifact.is_dir() or (directory.endswith('/node-modules') and artifact.suffix == '.tar'):
                    yield artifact
    for root in (Path('/root'), Path('/root/projects')):
        for parent in root.iterdir():
            if parent.is_dir() and not parent.is_symlink():
                for nested in (parent / '.claude/worktrees', parent / '.qa-wt'):
                    if nested.is_dir() and not nested.is_symlink():
                        for worktree in nested.iterdir():
                            if worktree.is_dir() and not worktree.is_symlink():
                                for dependency in ('node_modules', '.venv', 'venv'):
                                    build = worktree / dependency
                                    if build.is_dir() and not build.is_symlink():
                                        yield build
    release_root = Path('/opt/gen-workflow-staging/releases')
    if release_root.is_dir():
        releases = sorted((p for p in release_root.iterdir() if p.is_dir() and not p.is_symlink()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        current = Path('/opt/gen-workflow-staging/current').resolve(strict=True)
        # Keep the live release and five recent versions even while root is under pressure.
        yield from (p for p in releases[5:] if p.resolve() != current)
    for root in (Path('/var/log'), Path('/home/ubuntu/gen-backend-v2/shared/log')):
        for p in root.glob('*.log*.gz'):
            yield p
        for p in root.glob('*.log.[0-9]'):
            yield p


def docker_cleanup(building, rows, dry):
    if building:
        log('KEEP Docker: active build')
        return
    operations = [['docker', 'builder', 'prune', '-f', '--filter', 'until=24h'],
                  ['docker', 'container', 'prune', '-f', '--filter', 'until=24h',
                   '--filter', 'label!=com.docker.compose.project',
                   '--filter', 'label=qa.disk-prune=true'],
                  ['docker', 'image', 'prune', '-f', '--filter', 'until=24h']]
    # Unlabelled stopped containers may contain a database or service state; retain them.
    # Tagged local CI/plugin images are required between builds with pull:false; never prune them.
    in_use = {row['Image'] for row in rows}
    image_ids = command(['docker', 'image', 'ls', '-q']).split()
    images = inspect_objects(sorted(set(image_ids)), image=True)
    for image in images:
        tags = image['RepoTags'] or []
        if image['Id'] in in_use or any(
            tag.startswith(('woodpeckerci/', 'mac-', 'gbv2-ci:', 'limitless-data-ci:', 'day-ci:'))
            or '-ci:' in tag or '-ci-' in tag or '-qa' in tag or 'cifix' in tag for tag in tags
        ):
            continue
        created = dt.datetime.fromisoformat(image['Created'].replace('Z', '+00:00')).timestamp()
        if time.time() - created >= AGE:
            operations.append(['docker', 'image', 'rm', *(tags or [image['Id']])])
    for args in operations:
        log(('DRY ' if dry else 'RUN ') + ' '.join(args))
        if not dry:
            try:
                log(command(args, 120).strip())
            except subprocess.SubprocessError as exc:
                log(f'KEEP Docker operation refused: {exc}')
    # No volume operation belongs here: "unused" never proves database data disposable.


def root_state():
    # df uses space available to services, excluding ext4's reserved blocks.
    st = os.statvfs('/')
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    available = st.f_bavail * st.f_frsize
    return 100 * used / (used + available), available


def run(dry):
    used, available = root_state()
    log(f'START root_used={used:.1f}% available_bytes={available} dry={dry}')
    if used < 75 and not dry:
        log('NO_PRESSURE root below 75%')
        return
    held, mounts, building, rows = activity()
    docker_cleanup(building, rows, dry)
    now = time.time()
    for path in candidates():
        used, available = root_state()
        if used < 69 and not dry:
            break
        if not safe(path, held, mounts, now):
            log(f'KEEP {path}')
            continue
        log(f'{"DRY" if dry else "REMOVE"} path={path}')
        if not dry:
            before = available
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                log(f'KEEP vanished {path}')
                continue
            except OSError as exc:
                log(f'KEEP removal refused {path}: {exc}')
                continue
            used, available = root_state()
            log(f'AFTER path={path} free_delta_bytes={available - before} root_used={used:.1f}% available_bytes={available}')
    log('END ' + command(['df', '-h', '/', '/mnt/data']).strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    with open('/run/qa-disk-prune.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('BUSY existing prune owns lock')
            return
        run(args.dry_run)


if __name__ == '__main__':
    main()
