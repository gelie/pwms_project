"""
python manage.py seed_structure [--dry-run]
Idempotent. Seeds: 7th Parliament (legislative tree) + Parliament Administration.
Sources: Parliament high-level organogram (31 Mar 2024) and parliament.gov.za/divisions.

``--dry-run`` runs the same seeding inside the transaction and rolls it back, so
the reported counts are the ones a real run would produce.
"""

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from pwms.models.groups import Group, ParliamentTerm

T = Group.Type


def N(name, short="", gtype=T.UNIT, *children, ext=None):
    return (name, short, gtype, children, ext)


def C(ext, name):
    """A committee leaf. ext = committee ID on parliament.gov.za/committee-details/<ID>."""
    return N(name, "", T.COMMITTEE, ext=ext)


PROVINCES = [
    ("Eastern Cape", "EC"),
    ("Free State", "FS"),
    ("Gauteng", "GP"),
    ("KwaZulu-Natal", "KZN"),
    ("Limpopo", "LP"),
    ("Mpumalanga", "MP"),
    ("Northern Cape", "NC"),
    ("North West", "NW"),
    ("Western Cape", "WC"),
]


# ---- Committees (IDs from parliament.gov.za/committee-chairpersons) ---------
NA_PORTFOLIO = [
    (252, "Agriculture"),
    (253, "Basic Education"),
    (254, "Communications and Digital Technologies"),
    (255, "Cooperative Governance and Traditional Affairs"),
    (256, "Correctional Services"),
    (257, "Defence and Military Veterans"),
    (258, "Electricity and Energy"),
    (259, "Employment and Labour"),
    (260, "Forestry, Fisheries and Environment"),
    (261, "Health"),
    (262, "Higher Education"),
    (263, "Home Affairs"),
    (264, "Human Settlements"),
    (265, "International Relations and Cooperation"),
    (266, "Justice and Constitutional Development"),
    (267, "Land Reform and Rural Development"),
    (268, "Mineral and Petroleum Resources"),
    (269, "Planning, Monitoring and Evaluation"),
    (270, "Police"),
    (271, "Public Service and Administration"),
    (272, "Public Works and Infrastructure"),
    (273, "Science, Technology and Innovation"),
    (274, "Small Business Development"),
    (275, "Social Development"),
    (276, "Sport, Arts and Culture"),
    (277, "Tourism"),
    (278, "Trade, Industry and Competition"),
    (279, "Transport"),
    (280, "Water and Sanitation"),
    (281, "Women, Youth and Persons with Disabilities"),
]
NA_OTHER = [
    (282, "Standing Committee on Appropriations"),
    (283, "Standing Committee on Finance"),
    (284, "Standing Committee on Public Accounts"),
    (285, "Standing Committee on the Auditor-General"),
    (305, "Committee on The Presidency"),
    (306, "Executive Undertakings and Petitions Committee"),
    (247, "Multi-Party Women’s Caucus"),
]
# Previously without IDs; supplied from the committee list.
NA_MORE = [
    (
        304,
        "Ad Hoc Committee to Investigate Allegations made by Lieutenant General Nhlanhla Mkhwanazi",
    ),
    (307, "Impeachment Committee for Section 89 Enquiry"),
    (303, "Mediation Committee"),
    (302, "Members’ Support Forum"),
    (299, "Parliamentary Group on International Relations"),
    (251, "Programme Committee of the National Assembly"),
    (301, "Standing Committee on Powers and Privileges"),
]
NCOP_SELECT = [
    (289, "Agriculture, Land Reform and Mineral Resources"),
    (290, "Appropriations"),
    (291, "Cooperative Governance and Public Administration"),
    (292, "Economic Development and Trade"),
    (293, "Education, Sciences and Creative Industries"),
    (294, "Finance"),
    (295, "Public Infrastructure and Ministries in Presidency"),
    (296, "Public Petitions and Executive Undertakings"),
    (297, "Security and Justice"),
    (298, "Social Services"),
]
JOINT = [
    (243, "Joint Committee on Ethics and Members’ Interests"),
    (244, "Joint Standing Committee on Intelligence"),
    (245, "Joint Standing Committee on Defence"),
    (246, "Joint Standing Committee on the Financial Management of Parliament"),
    (248, "Constitutional Review Committee"),
]


# ---- Legislative (created once per term) ------------------------------------
def legislative_tree(term_name):
    return N(
        term_name,
        "PARL",
        T.PARLIAMENT,
        N(
            "National Assembly",
            "NA",
            T.HOUSE,  # Speaker, Deputy Speaker = roles
            # Membership targets: members join these directly, and each party
            # nests under the Parties container (see MembershipSyncService).
            N("NA Members", "NA-MEM", T.MEMBERS),
            N("NA Parties", "NA-PTY", T.PARTIES),
            N(
                "NA Committees",
                "NA-COM",
                T.COMMITTEES,
                *[C(i, f"Portfolio Committee on {n}") for i, n in NA_PORTFOLIO],
                *[C(i, n) for i, n in NA_OTHER],
                *[C(i, n) for i, n in NA_MORE],
                N(
                    "Rules Committee of the National Assembly",
                    "",
                    T.COMMITTEE,
                    C(250, "Subcommittee on the Review of the National Assembly Rules"),
                    ext=249,
                ),
            ),
        ),
        N(
            "National Council of Provinces",
            "NCOP",
            T.HOUSE,  # Chair, Deputy Chair = roles
            N("NCOP Members", "NCOP-MEM", T.MEMBERS),
            N("NCOP Parties", "NCOP-PTY", T.PARTIES),
            N(
                "NCOP Committees",
                "NCOP-COM",
                T.COMMITTEES,
                *[C(i, f"Select Committee on {n}") for i, n in NCOP_SELECT],
                C(288, "Programme Committee of the NCOP"),
                N(
                    "Rules Committee of the NCOP",
                    "",
                    T.COMMITTEE,
                    C(287, "Subcommittee on the Review of Council Rules"),
                    ext=286,
                ),
            ),
            *[N(f"{p} Delegation", f"NCOP-{c}", T.DELEGATION) for p, c in PROVINCES],
        ),
        N("Joint Committees", "JNT-COM", T.COMMITTEES, *[C(i, n) for i, n in JOINT]),
    )


# ---- Administration (permanent) ---------------------------------------------
ADMIN = N(
    "Parliament Administration",
    "ADMIN",
    T.ADMINISTRATION,
    N(
        "Secretary to Parliament",
        "SEC",
        T.OFFICE,
        # Everything below the container reports to the Secretary: the two
        # Deputy Secretary branches and the units that belong to neither.
        N(
            "Offices and Divisions",
            "O&D",
            T.BRANCH,
            N("Offices of the Speaker NA & Chairperson NCOP", "OSC", T.OFFICE),
            N("Legislative Sector Support Programme", "LSS", T.DIVISION),
            N("Parliamentary Budget Office", "PBO", T.OFFICE),
            N("Strategic Management & Governance", "SMG", T.DIVISION),
            N(
                "Financial Management Office",
                "CFO",
                T.OFFICE,
                N(
                    "Finance Management Section",
                    "FIN",
                    T.SECTION,
                    N("Management Accounting Unit"),
                    N("Financial Accounting Unit"),
                ),
                N(
                    "Supply Chain Management Section",
                    "SCM",
                    T.SECTION,
                    N("Risk, Demand and Planning Unit"),
                    N("Strategic Sourcing and Acquisitions Unit"),
                    N("Logistics, Fixed Assets and Disposal Management Unit"),
                ),
            ),
            N("Internal Audit Office", "IA", T.OFFICE),
            N(
                "Risk Management Office", "RISK", T.OFFICE
            ),  # web: "Governance, Risk and Compliance"
            N("Registrar of Members’ Interests Office", "ROMI", T.OFFICE),
            N("Treasury Advice Office", "TAO", T.OFFICE),
        ),
        N(
            "Core Business Branch",
            "CBB",
            T.BRANCH,  # Deputy Secretary: Core Business
            N(
                "National Assembly Division",
                "NAD",
                T.DIVISION,
                N("House Plenaries & Procedure"),
                N("Research & Parliamentary Practice"),
                N("Serjeant-at-Arms"),
            ),
            N(
                "NCOP Division",
                "NCOPD",
                T.DIVISION,
                N("House Plenaries & Procedure"),
                N("Provincial & Municipal Liaison"),
                N("Office of the Chief Whip"),
            ),
            N(
                "Core Business Support Division",
                "CBS",
                T.DIVISION,
                N("Legal Services"),
                N("Committee Support Services"),
                N("Public Participation Services"),
                N("OLGB"),
            ),
            N(
                "Knowledge & Information Services Division",
                "KIS",
                T.DIVISION,
                N("Research Services"),
                N("Information Services Centre (Library)"),
                N("Documentation Services"),
                N("Language Services"),
            ),
            N(
                "International Relations & Protocol Division",
                "IRP",
                T.DIVISION,
                N("IR Policy Analysis"),
                N("Multi-Lateral Relations"),
                N("Protocol & Ceremonial Services"),
                N("Operations Management"),
            ),
        ),
        N(
            "Support Services Branch",
            "SSB",
            T.BRANCH,  # Deputy Secretary: Support Services
            N(
                "Parliamentary Communication Services Division",
                "PCS",
                T.DIVISION,
                N("Multimedia Productions Services"),
                N("Information & Content Development Services"),
                N("Public Relations Services"),
                N("Media Relations Services"),
                N("Internal Communication Services"),
            ),
            N(
                "Human Resource Management Division",
                "HR",
                T.DIVISION,
                N("HR Business Partnering Services"),
                N("HR Shared Services"),
                N("Org Effectiveness Services"),
                N("Labour Relations Services"),
            ),
            N(
                "Members Support Services Division",
                "MSS",
                T.DIVISION,
                N("Members Facilities"),
                N("Members Services"),
            ),
            N(
                "ICT Division",
                "ICT",
                T.DIVISION,
                N("ICT Systems and Applications"),
                N("Broadcast & Technical Support Services"),
                N("IT Infrastructure Services"),
                N("Enterprise Resource Planning"),
                N("Management Information Systems"),
            ),
            N(
                "Institutional Support Services Division",
                "ISS",
                T.DIVISION,
                N("Household Services"),
                N("Catering Services"),
                N("Safety, Health & Environment Services"),
                N("Artworks"),
            ),
            N("Protection Services Division", "PSD", T.DIVISION),
        ),
    ),
)


def build(node, parent=None, term=None, stats=None):
    name, short, gtype, children, ext = node
    if ext is not None:
        # Committees: identity is the parliament.gov.za ID, so renames or
        # re-parenting update the existing row instead of creating a duplicate.
        obj, created = Group.objects.update_or_create(
            term=term,
            external_id=ext,
            defaults={
                "name": name,
                "short_name": short,
                "group_type": gtype,
                "parent": parent,
            },
        )
        outcome = "created" if created else "updated"
    else:
        # A coded node is matched by its code, so a rename or a move under a
        # different parent updates that row instead of leaving the old one
        # behind and creating a second one. Uncoded leaves still match by
        # (name, parent, term), the model's natural key.
        obj = (
            Group.objects.filter(term=term, short_name=short).first() if short else None
        )
        if obj is None:
            obj, created = Group.objects.get_or_create(
                name=name,
                parent=parent,
                term=term,
                defaults={"short_name": short, "group_type": gtype},
            )
            if created:
                outcome = "created"
            elif short and not obj.short_name:
                obj.short_name = short
                obj.save(update_fields=["short_name"])
                outcome = "updated"
            else:
                outcome = "existing"
        else:
            parent_id = parent.pk if parent is not None else None
            if (obj.name, obj.group_type, obj.parent_id) != (
                name,
                gtype,
                parent_id,
            ):
                obj.name = name
                obj.group_type = gtype
                obj.parent = parent
                obj.save(update_fields=["name", "group_type", "parent"])
                outcome = "updated"
            else:
                outcome = "existing"
    if stats is not None:
        stats[outcome] += 1
        stats["nodes"] += 1
    for child in children:
        build(child, obj, term, stats)
    return obj


class Command(BaseCommand):
    help = "Seed the legislative (per-term) and administration group trees."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the seed inside the transaction and roll it back, so the "
                "reported counts are exact and nothing is written."
            ),
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        stats = {"created": 0, "existing": 0, "updated": 0, "nodes": 0}

        # One transaction either way: `--dry-run` executes the same work and then
        # rolls it back, so its counts are the ones a real run would produce.
        with transaction.atomic():
            # TODO: confirm exact dates; end_date = date of the next election cycle.
            term, term_created = ParliamentTerm.objects.get_or_create(
                number=7,
                defaults={
                    "name": "7th Parliament",
                    "start_date": datetime.date(2024, 6, 14),
                    "end_date": datetime.date(2029, 6, 13),
                },
            )
            # Re-parenting is bulk work: MPTT's per-save moves cannot keep up
            # when many siblings change parent in one pass, so the coordinates
            # are recomputed once afterwards instead.
            with Group.objects.disable_mptt_updates():
                build(legislative_tree(term.name), term=term, stats=stats)
                build(ADMIN, stats=stats)
            # Skipped on a dry run: the tree only has to be left consistent in
            # rows that will actually be committed.
            if not dry_run:
                Group.objects.rebuild()
            if dry_run:
                transaction.set_rollback(True)

        if dry_run:
            term_status = "would be created" if term_created else "already present"
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN – nothing written. {stats['nodes']} node(s): "
                    f"{stats['created']} would be created, "
                    f"{stats['updated']} row(s) updated, "
                    f"{stats['existing']} already present. "
                    f"ParliamentTerm {term.number} ({term.name}) {term_status}."
                )
            )
        else:
            term_status = "created" if term_created else "already present"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Structure seeded. {stats['nodes']} node(s): "
                    f"{stats['created']} created, {stats['updated']} row(s) "
                    f"updated, {stats['existing']} already present. "
                    f"ParliamentTerm {term.number} ({term.name}) {term_status}."
                )
            )
