"""
SONiC 'show' CLI plugin for PathTrace.

Installed by sonic-package-manager at /cli/show/plugins/pathtrace.py and auto-discovered
by SONiC's click-based CLI. Adds:

    show pathtrace datapath --intent "<intent>" [--deps]
    show pathtrace audit <type|all>
    show pathtrace types

The plugin shells into the pathtrace container to run the tool against the live databases.
"""

import click
import subprocess

# The container name sonic-package-manager assigns matches the package/service name.
_CONTAINER = "pathtrace"


def _run_in_container(args):
    """Run `pathtrace <args>` inside the pathtrace container and stream output."""
    cmd = ["docker", "exec", _CONTAINER, "pathtrace"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            click.echo(proc.stdout, nl=False)
        if proc.stderr:
            click.echo(proc.stderr, nl=False)
        return proc.returncode
    except FileNotFoundError:
        click.echo("error: docker not available on host", err=True)
        return 2


@click.group(name="pathtrace")
def pathtrace():
    """Trace intent propagation across the SONiC datapath."""
    pass


@pathtrace.command(name="datapath")
@click.option("--intent", required=True,
              help='Object to trace, e.g. "acl rule BLOCK_LIST RULE_10", "route 10.0.0.0/24"')
@click.option("--deps", is_flag=True,
              help="Also trace the objects this one depends on and name the root cause")
@click.option("--detailed", is_flag=True,
              help="Show the full pipeline view + correlated logs (default: compact summary)")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def datapath(intent, deps, detailed, as_json):
    """Trace one intent across CONFIG_DB -> APPL_DB -> STATE_DB -> ASIC_DB."""
    args = ["--intent", intent, "--force-color"]
    if deps:
        args.append("--deps")
    if detailed:
        args.append("--detailed")
    if as_json:
        args.append("--json")
    rc = _run_in_container(args)
    # Surface the trace verdict as the command's exit status (0 healthy, 1 broken).
    raise SystemExit(rc)


@pathtrace.command(name="audit")
@click.argument("object_type")
@click.option("--deps", is_flag=True,
              help="Also trace each broken object's dependencies and collapse shared "
                   "upstream failures into fleet-wide root-cause findings")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def audit(object_type, deps, as_json):
    """Sweep every CONFIG_DB object of a type (or 'all') and list those not in ASIC_DB."""
    args = ["--audit", object_type, "--force-color"]
    if deps:
        args.append("--deps")
    if as_json:
        args.append("--json")
    raise SystemExit(_run_in_container(args))


@pathtrace.command(name="types")
def types():
    """List traceable object types (from the object_types.yaml registry)."""
    _run_in_container(["--list-types"])


def register(cli):
    """SONiC CLI plugin entrypoint: attach this group under `show`."""
    cli.add_command(pathtrace)
