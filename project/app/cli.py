"""Local operator commands; never registered as HTTP routes."""

import click
from flask.cli import with_appcontext

from app.services.officer_approval_service import approve_officer
from app.services.officer_registration_service import normalize_officer_email
from app.services.farmer_assignment_service import assign_farmer
from app.services.registration_service import normalize_kenyan_phone


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


@click.command("assign-farmer")
@click.option("--farmer-phone", required=True, help="Farmer's registered Kenyan phone number.")
@click.option("--officer-email", required=True, help="Approved officer's registered email.")
@with_appcontext
def assign_farmer_command(farmer_phone, officer_email):
    """Assign an unassigned farmer. For authorized local operators only."""
    phone = normalize_kenyan_phone(farmer_phone)
    email = normalize_officer_email(officer_email)
    if phone is None or email is None:
        raise click.ClickException("Provide a valid farmer phone number and officer email address.")
    click.confirm(f"Assign farmer {phone} to officer {email}?", abort=True)
    result = assign_farmer(phone, email)
    if not result["ok"]:
        raise click.ClickException(result["message"])
    click.echo(result["message"])
