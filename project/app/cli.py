"""Local operator commands; never registered as HTTP routes."""

import click
from flask.cli import with_appcontext

from app.services.officer_approval_service import approve_officer
from app.services.officer_registration_service import normalize_officer_email


@click.command("approve-officer")
@click.option("--email", required=True, help="Registered email of the pending officer.")
@with_appcontext
def approve_officer_command(email):
    """Approve a pending officer. For authorized local operators only."""
    normalized = normalize_officer_email(email)
    if normalized is None:
        raise click.ClickException("Provide a valid registered officer email address.")
    click.confirm(f"Approve the pending officer registered as {normalized}?", abort=True)
    result = approve_officer(normalized)
    if not result["ok"]:
        raise click.ClickException(result["message"])
    click.echo(result["message"])
