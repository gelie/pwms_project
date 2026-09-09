import logging
import re
import time

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from workflows.models import Group, GroupMembership, Role, User

# Set up logging
logger = logging.getLogger("committee_scraper")
logger.setLevel(logging.INFO)

# Create file handler
file_handler = logging.FileHandler(settings.LOG_DIR / "committee_scraping.log")
file_handler.setLevel(logging.INFO)

# Create console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Create formatter
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers to logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = "Scrape committee information from parliament.gov.za and populate database with members"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run without making database changes",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging",
        )
        parser.add_argument(
            "--import-committees",
            action="store_true",
            help="Import committees first using existing command",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.verbose = options["verbose"]
        self.import_committees = options["import_committees"]

        # Statistics tracking
        self.stats = {
            "committees_processed": 0,
            "users_found": 0,
            "users_not_found": 0,
            "memberships_created": 0,
            "memberships_updated": 0,
            "memberships_skipped": 0,
            "errors": 0,
            "missing_groups": [],
            "missing_users": [],
        }

        self.stdout.write(
            self.style.SUCCESS("Starting Parliament committee scraping process...")
        )

        # Import committees first if requested
        if self.import_committees:
            self.run_import_committees()

        # Create generic committee roles
        self.create_committee_roles()

        # Scrape committee chairpersons and members
        self.scrape_committee_data()

        # Print final statistics
        self.print_statistics()

        self.stdout.write(
            self.style.SUCCESS("Parliament committee scraping completed!")
        )

    def run_import_committees(self):
        """Run the existing import_committees command"""
        from django.core.management import call_command

        self.stdout.write("Running import_committees command first...")
        try:
            call_command("import_committees", verbosity=1)
            self.stdout.write(self.style.SUCCESS("Committees imported successfully"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error importing committees: {e}"))
            logger.error(f"Error importing committees: {e}")

    def create_committee_roles(self):
        """Create generic committee roles"""
        committee_roles = [
            ("Committee Chairperson", "Chairperson of a parliamentary committee"),
            (
                "Committee Deputy Chairperson",
                "Deputy chairperson of a parliamentary committee",
            ),
            ("Committee Member", "Member of a parliamentary committee"),
            (
                "Alternate Committee Member",
                "Alternate member of a parliamentary committee",
            ),
            ("Committee Secretary", "Secretary of a parliamentary committee"),
            ("Committee Researcher", "Researcher for a parliamentary committee"),
            ("Committee Advisor", "Advisor to a parliamentary committee"),
        ]

        created_roles = []
        for role_name, description in committee_roles:
            if not self.dry_run:
                role, created = Role.objects.get_or_create(
                    name=role_name, defaults={"description": description}
                )
                if created:
                    created_roles.append(role_name)
                    logger.info(f"Created role: {role_name}")
            else:
                self.stdout.write(f"[DRY RUN] Would create role: {role_name}")

        if created_roles:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created {len(created_roles)} new roles: {', '.join(created_roles)}"
                )
            )

    def scrape_committee_data(self):
        """Main scraping method"""
        # Scrape chairpersons page
        chairpersons_data = self.scrape_committee_chairpersons()

        # Process each committee
        for data in chairpersons_data:
            try:
                self.process_committee_data(data)
                self.stats["committees_processed"] += 1
                time.sleep(1)  # Be respectful to the server
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(
                    f"Error processing committee {data.get('committee_name', 'unknown')}: {e}"
                )

    def scrape_committee_chairpersons(self):
        """Scrape committee chairpersons from parliament.gov.za"""
        url = "https://www.parliament.gov.za/committee-chairpersons"

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "html.parser")

                # Extract committee data using a more robust approach
                committee_data = self.extract_committee_chairperson_data(soup)

                self.stdout.write(
                    f"Found {len(committee_data)} committees with chairpersons"
                )
                logger.info(f"Found {len(committee_data)} committees with chairpersons")

                return committee_data

        except httpx.HTTPError as e:
            self.stdout.write(
                self.style.ERROR(f"Error fetching committee chairpersons: {e}")
            )
            logger.error(f"Error fetching committee chairpersons: {e}")
            return []

    def extract_committee_chairperson_data(self, soup):
        """Extract committee and chairperson data using regex patterns"""
        committee_data = []

        # Find all links
        committee_links = {}
        person_links = {}

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)

            if re.search(r"/committee-details/\d+", href) and text:
                # Handle both relative and absolute URLs
                if href.startswith("http"):
                    committee_links[text] = href
                else:
                    committee_links[text] = f"https://www.parliament.gov.za{href}"
            elif re.search(r"/person-details/\d+", href) and text:
                # Handle both relative and absolute URLs
                if href.startswith("http"):
                    person_links[text] = href
                else:
                    person_links[text] = f"https://www.parliament.gov.za{href}"

        logger.info(
            f"Found {len(committee_links)} committee links and {len(person_links)} person links"
        )

        # Parse the page content more systematically
        # Look for the main content area
        main_content = soup.find("main") or soup.find("div", class_="content") or soup
        text_content = main_content.get_text()

        # Split into lines and process
        lines = [line.strip() for line in text_content.split("\n") if line.strip()]

        # Match chairpersons to committees based on the page structure
        # The pattern is typically: Person Name followed by Committee Name
        i = 0
        while i < len(lines):
            line = lines[i]

            # Check if this line contains a person name
            person_name = None
            person_url = None
            for name, url in person_links.items():
                if name in line:
                    person_name = name
                    person_url = url
                    break

            if person_name:
                # Look for committee in the next few lines
                for j in range(i + 1, min(i + 5, len(lines))):
                    next_line = lines[j]
                    committee_name = None
                    committee_url = None

                    for name, url in committee_links.items():
                        if name in next_line or next_line in name:
                            committee_name = name
                            committee_url = url
                            break

                    if committee_name:
                        committee_data.append(
                            {
                                "chairperson_name": person_name,
                                "chairperson_url": person_url,
                                "committee_name": committee_name,
                                "committee_url": committee_url,
                            }
                        )
                        break

            i += 1

        # Remove duplicates
        seen = set()
        unique_data = []
        for item in committee_data:
            key = (item["chairperson_name"], item["committee_name"])
            if key not in seen:
                seen.add(key)
                unique_data.append(item)

        return unique_data

    def process_committee_data(self, data):
        """Process individual committee data"""
        committee_name = data["committee_name"]
        chairperson_name = data["chairperson_name"]
        committee_url = data["committee_url"]
        chairperson_url = data["chairperson_url"]

        if self.verbose:
            self.stdout.write(f"Processing: {committee_name} - {chairperson_name}")

        # Find or log missing committee
        committee_group = self.find_committee_group(committee_name)
        if not committee_group:
            self.stats["missing_groups"].append(committee_name)
            logger.warning(f"Committee group not found: {committee_name}")
            return

        # Process chairperson
        chairperson_user = self.find_existing_user_only(
            chairperson_name, chairperson_url
        )
        if chairperson_user:
            self.create_membership(
                chairperson_user, committee_group, "Committee Chairperson"
            )

        # Get additional committee members
        committee_members = self.scrape_committee_members(committee_url)

        # Process other committee members
        for member_data in committee_members:
            if member_data["name"] != chairperson_name:  # Don't duplicate chairperson
                user = self.find_existing_user_only(
                    member_data["name"], member_data["url"]
                )
                if user:
                    role = member_data.get("role", "Committee Member")
                    section = member_data.get("section", "general")

                    if self.verbose:
                        self.stdout.write(
                            f"  Adding {member_data['name']} as {role} (from {section} section)"
                        )

                    self.create_membership(user, committee_group, role)
                else:
                    # Skip membership creation if user not found
                    self.stats["memberships_skipped"] += 1

    def find_committee_group(self, committee_name):
        """Find committee group by name with fuzzy matching, create if missing"""
        if self.dry_run:
            return True  # Assume it exists for dry run

        # Try exact match first
        group = Group.objects.filter(name__iexact=committee_name).first()
        if group:
            return group

        # Try partial matches
        for group in Group.objects.filter(name__icontains="committee"):
            if self.names_similar(group.name, committee_name):
                logger.info(
                    f'Matched "{committee_name}" to existing group "{group.name}"'
                )
                return group

        # If no match found, create the missing committee
        logger.info(f"Committee not found in database, creating: {committee_name}")
        return self.create_missing_committee(committee_name)

    def names_similar(self, name1, name2):
        """Check if two committee names are similar enough to be considered the same"""
        # Normalize names
        name1_clean = re.sub(r"[^\w\s]", "", name1.lower())
        name2_clean = re.sub(r"[^\w\s]", "", name2.lower())

        # Remove common words
        common_words = {"committee", "on", "and", "the", "of", "for", "in"}
        name1_words = set(name1_clean.split()) - common_words
        name2_words = set(name2_clean.split()) - common_words

        # Check overlap
        if len(name1_words) == 0 or len(name2_words) == 0:
            return False

        overlap = len(name1_words & name2_words)
        similarity = overlap / max(len(name1_words), len(name2_words))

        return similarity > 0.6  # 60% similarity threshold

    def create_missing_committee(self, committee_name):
        """Create a missing committee group based on scraped data"""
        try:
            # Determine committee type based on name
            committee_type = self.determine_committee_type(committee_name)

            # Generate short name (max 50 chars)
            short_name = committee_name
            if len(short_name) > 50:
                # Try to abbreviate common words
                abbreviations = {
                    "Committee": "Cttee",
                    "Portfolio": "PC",
                    "Select": "SC",
                    "Standing": "SC",
                    "Joint": "JC",
                    "and": "&",
                    "Development": "Dev",
                    "International": "Intl",
                    "Administration": "Admin",
                    "Constitutional": "Const",
                }

                for full_word, abbrev in abbreviations.items():
                    short_name = short_name.replace(full_word, abbrev)

                # If still too long, truncate
                if len(short_name) > 50:
                    short_name = short_name[:47] + "..."

            # Determine parent group
            parent_group = self.get_or_create_parent_group(
                committee_name, committee_type
            )

            # Create the group
            group = Group.objects.create(
                name=committee_name,
                short_name=short_name,
                description=f"Parliamentary committee: {committee_name}",
                group_type=committee_type,
                parent=parent_group,
                is_active=True,
                start_date=timezone.now().date(),
            )

            self.stdout.write(
                self.style.SUCCESS(f"Created missing committee: {committee_name}")
            )
            logger.info(
                f"Created missing committee: {committee_name} (type: {committee_type})"
            )

            # Track in statistics
            if "committees_created" not in self.stats:
                self.stats["committees_created"] = 0
            self.stats["committees_created"] += 1

            return group

        except Exception as e:
            logger.error(f"Error creating committee {committee_name}: {e}")
            self.stats["missing_groups"].append(committee_name)
            return None

    def determine_committee_type(self, committee_name):
        """Determine committee type based on name patterns"""
        name_lower = committee_name.lower()

        if "portfolio committee" in name_lower:
            return "portfolio_committee"
        elif "select committee" in name_lower:
            return "select_committee"
        elif (
            "joint standing committee" in name_lower or "joint committee" in name_lower
        ):
            return "joint_committee"
        elif "standing committee" in name_lower:
            return "internal_committee"
        elif "constitutional review" in name_lower:
            return "special_committee"
        elif "ad hoc" in name_lower:
            return "ad_hoc_committee"
        elif "public accounts" in name_lower:
            return "public_accounts_committee"
        elif "multi party" in name_lower or "caucus" in name_lower:
            return "special_committee"
        else:
            return "internal_committee"  # Default

    def get_or_create_parent_group(self, committee_name, committee_type):
        """Get or create appropriate parent group for committee"""
        name_lower = committee_name.lower()

        # Determine parent based on committee type and name
        if committee_type == "joint_committee" or "joint" in name_lower:
            parent_name = "Joint"
        elif committee_type == "select_committee" or "select committee" in name_lower:
            parent_name = "National Council of Provinces"
        else:
            parent_name = "National Assembly"

        # Get or create parent group
        parent_group, created = Group.objects.get_or_create(
            name=parent_name,
            defaults={
                "short_name": parent_name,
                "description": f"{parent_name} parliamentary house",
                "group_type": "house",
                "is_active": True,
                "start_date": timezone.now().date(),
            },
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created parent group: {parent_name}")
            )
            logger.info(f"Created parent group: {parent_name}")

        return parent_group

    def scrape_committee_members(self, committee_url):
        """Scrape detailed committee membership from committee page, handling Composition and Alternate sections"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            with httpx.Client(timeout=30.0) as client:
                response = client.get(committee_url, headers=headers)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "html.parser")

                members = []

                # Look for Composition and Alternate sections
                composition_members = self.extract_members_from_section(
                    soup, "composition", "Committee Member"
                )
                alternate_members = self.extract_members_from_section(
                    soup, "alternate", "Alternate Committee Member"
                )

                members.extend(composition_members)
                members.extend(alternate_members)

                # If no structured sections found, fall back to general member extraction
                if not members:
                    members = self.extract_members_general(soup)
                    logger.info(
                        "No structured sections found, using general extraction"
                    )
                else:
                    logger.info(
                        f"Found {len(composition_members)} composition members and {len(alternate_members)} alternate members"
                    )

                logger.info(
                    f"Total {len(members)} members for committee: {committee_url}"
                )
                return members

        except httpx.HTTPError as e:
            logger.error(f"Error fetching committee members from {committee_url}: {e}")
            return []

    def extract_members_from_section(self, soup, section_name, default_role):
        """Extract members from a specific section (Composition or Alternate)"""
        members = []

        # Look for headings that contain the section name
        section_headings = soup.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6"],
            string=re.compile(section_name, re.IGNORECASE),
        )

        # Also look for divs or sections with class/id containing the section name
        section_containers = soup.find_all(
            ["div", "section"], attrs={"class": re.compile(section_name, re.IGNORECASE)}
        )
        section_containers.extend(
            soup.find_all(
                ["div", "section"],
                attrs={"id": re.compile(section_name, re.IGNORECASE)},
            )
        )

        all_sections = section_headings + section_containers

        for section in all_sections:
            # Find all member links within this section
            if section.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                # For headings, look for members in the following siblings
                current = section.next_sibling
                while current and current.name not in [
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                ]:
                    if hasattr(current, "find_all"):
                        member_links = current.find_all(
                            "a", href=re.compile(r"/person-details/\d+")
                        )
                        for link in member_links:
                            member = self.extract_member_from_link(
                                link, default_role, section_name
                            )
                            if member:
                                members.append(member)
                    current = current.next_sibling
            else:
                # For containers, look for members within the container
                member_links = section.find_all(
                    "a", href=re.compile(r"/person-details/\d+")
                )
                for link in member_links:
                    member = self.extract_member_from_link(
                        link, default_role, section_name
                    )
                    if member:
                        members.append(member)

        return members

    def extract_member_from_link(self, link, default_role, section_context):
        """Extract member information from a link element"""
        member_url = link.get("href")
        member_name = link.get_text(strip=True)

        if not member_name or len(member_name) <= 2:
            return None

        # Handle both relative and absolute URLs
        if member_url.startswith("http"):
            full_url = member_url
        else:
            full_url = f"https://www.parliament.gov.za{member_url}"

        # Determine role from context
        role = default_role
        context = ""

        # Check parent elements for role indicators
        parent = link.parent
        depth = 0
        while parent and depth < 3:  # Limit depth to avoid too much context
            parent_text = parent.get_text().lower()
            context += parent_text

            # Check for chairperson indicators
            if "chair" in parent_text and "deputy" not in parent_text:
                role = "Committee Chairperson"
                break
            elif "deputy" in parent_text and "chair" in parent_text:
                role = "Committee Deputy Chairperson"
                break

            parent = parent.parent
            depth += 1

        return {
            "name": member_name,
            "url": full_url,
            "role": role,
            "section": section_context,
        }

    def extract_members_general(self, soup):
        """General member extraction when no structured sections are found"""
        members = []
        member_links = soup.find_all("a", href=re.compile(r"/person-details/\d+"))

        seen_urls = set()
        for link in member_links:
            member_url = link.get("href")
            if member_url not in seen_urls:
                seen_urls.add(member_url)
                member = self.extract_member_from_link(
                    link, "Committee Member", "general"
                )
                if member:
                    members.append(member)

        return members

    def find_existing_user_only(self, full_name, profile_url):
        """Find existing user only - do not create new users"""
        name_parts = self.parse_full_name(full_name)

        if not self.dry_run:
            # Generate search key: lowercase(firstname[0] + lastname)
            search_key = self.generate_user_search_key(
                name_parts["first_name"], name_parts["last_name"]
            )

            # Try multiple search strategies to find existing user
            user = self.find_existing_user(name_parts, search_key)

            if user:
                self.stats["users_found"] += 1
                if self.verbose:
                    self.stdout.write(
                        f"Found existing user: {user.username} for {full_name}"
                    )
                return user
            else:
                # Log user not found
                self.stats["users_not_found"] += 1
                self.stats["missing_users"].append(
                    {
                        "full_name": full_name,
                        "profile_url": profile_url,
                        "parsed_name": name_parts,
                        "search_key": search_key,
                    }
                )
                logger.warning(
                    f"User not found in system: {full_name} (search key: {search_key})"
                )
                if self.verbose:
                    self.stdout.write(f"User not found: {full_name}")
                return None
        else:
            self.stdout.write(f"[DRY RUN] Would search for user: {full_name}")
            return None

    def generate_user_search_key(self, first_name, last_name):
        """Generate search key: lowercase(firstname[0] + last_word_of_lastname)"""
        if not first_name or not last_name:
            return None

        first_char = first_name[0].lower() if first_name else ""
        # Handle hyphenated names: split by both spaces and hyphens, take the last part
        name_parts = last_name.replace("-", " ").split()
        last_word = name_parts[-1].lower() if name_parts else ""
        return f"{first_char}{last_word}"

    def find_existing_user(self, name_parts, search_key):
        """Find existing user using multiple search strategies"""
        first_name = name_parts["first_name"]
        last_name = name_parts["last_name"]

        # Strategy 1: Exact match (case insensitive)
        user = User.objects.filter(
            first_name__iexact=first_name, last_name__iexact=last_name
        ).first()
        if user:
            return user

        # Strategy 2: Search by username pattern (firstname[0] + lastname)
        if search_key:
            user = User.objects.filter(username__iexact=search_key).first()
            if user:
                return user

            # Also try with underscores
            user = User.objects.filter(
                username__iexact=search_key.replace(" ", "_")
            ).first()
            if user:
                return user

        # Strategy 3: Fuzzy match on names (handle variations)
        # Remove common prefixes/suffixes and try again
        last_name_variations = [
            last_name,
            last_name.replace(" ", ""),
            last_name.replace("-", " "),
            last_name.split()[-1] if " " in last_name else last_name,  # Last word only
        ]

        for variation in last_name_variations:
            user = User.objects.filter(
                first_name__iexact=first_name, last_name__icontains=variation
            ).first()
            if user:
                return user

        return None

    def parse_full_name(self, full_name):
        """Parse full name into components"""
        titles = ["Mr", "Ms", "Mrs", "Dr", "Prof", "Hon", "Adv", "Rt"]

        parts = full_name.split()
        title = ""
        first_name = ""
        last_name = ""

        if parts:
            # Check for title
            if parts[0].rstrip(".") in titles:
                title = parts[0].rstrip(".").lower()
                parts = parts[1:]

            # Handle "Rt Hon" case
            if title == "rt" and parts and parts[0].rstrip(".").lower() == "hon":
                title = "rt_hon"
                parts = parts[1:]

            if len(parts) >= 2:
                first_name = parts[0]
                last_name = " ".join(parts[1:])
            elif len(parts) == 1:
                first_name = parts[0]

        return {"title": title, "first_name": first_name, "last_name": last_name}

    @transaction.atomic
    def create_membership(self, user, group, role_name):
        """Create group membership with role"""
        if not self.dry_run:
            try:
                role = Role.objects.get(name=role_name)

                membership, created = GroupMembership.objects.get_or_create(
                    user=user,
                    group=group,
                    defaults={
                        "role": role,
                        "is_active": True,
                        "start_date": timezone.now().date(),
                    },
                )

                if created:
                    self.stats["memberships_created"] += 1
                    if self.verbose:
                        self.stdout.write(
                            f"Created membership: {user} -> {group} as {role_name}"
                        )
                    logger.info(
                        f"Created membership: {user.username} -> {group.name} as {role_name}"
                    )
                elif membership.role != role:
                    old_role = membership.role.name
                    membership.role = role
                    membership.is_active = True
                    membership.save()
                    self.stats["memberships_updated"] += 1
                    if self.verbose:
                        self.stdout.write(
                            f"Updated membership: {user} -> {group} from {old_role} to {role_name}"
                        )
                    logger.info(
                        f"Updated membership: {user.username} -> {group.name} from {old_role} to {role_name}"
                    )

            except Role.DoesNotExist:
                logger.error(f"Role not found: {role_name}")
                self.stdout.write(self.style.ERROR(f"Role not found: {role_name}"))
        else:
            self.stdout.write(
                f"[DRY RUN] Would create membership: {user} -> {group} as {role_name}"
            )

    def print_statistics(self):
        """Print final statistics"""
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS("=== SCRAPING STATISTICS ==="))
        self.stdout.write("=" * 50)
        self.stdout.write(f"Committees processed: {self.stats['committees_processed']}")
        self.stdout.write(
            f"Committees created: {self.stats.get('committees_created', 0)}"
        )
        self.stdout.write(f"Users found in system: {self.stats['users_found']}")
        self.stdout.write(f"Users NOT found in system: {self.stats['users_not_found']}")
        self.stdout.write(f"Memberships created: {self.stats['memberships_created']}")
        self.stdout.write(f"Memberships updated: {self.stats['memberships_updated']}")
        self.stdout.write(
            f"Memberships skipped (user not found): {self.stats['memberships_skipped']}"
        )
        self.stdout.write(f"Errors encountered: {self.stats['errors']}")

        if self.stats["missing_groups"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nMissing Groups ({len(self.stats['missing_groups'])}):"
                )
            )
            for group in self.stats["missing_groups"]:
                self.stdout.write(f"  - {group}")

        if self.stats["missing_users"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nUsers NOT Found in System ({len(self.stats['missing_users'])}):"
                )
            )
            self.stdout.write("These users need to be synced from HR database:")
            for user_data in self.stats["missing_users"]:
                if isinstance(user_data, dict):
                    self.stdout.write(
                        f"  - {user_data['full_name']} (search key: {user_data['search_key']})"
                    )
                else:
                    self.stdout.write(f"  - {user_data}")

        # Log detailed missing users information
        if self.stats["missing_users"]:
            logger.warning(
                f"MISSING USERS REPORT - {len(self.stats['missing_users'])} users not found in system:"
            )
            for user_data in self.stats["missing_users"]:
                if isinstance(user_data, dict):
                    logger.warning(
                        f"Missing User: {user_data['full_name']} | "
                        f"Search Key: {user_data['search_key']} | "
                        f"Parsed: {user_data['parsed_name']} | "
                        f"Profile: {user_data['profile_url']}"
                    )
                else:
                    logger.warning(f"Missing User: {user_data}")

        logger.info(
            f"Scraping completed - Committees: {self.stats['committees_processed']}, "
            f"Committees created: {self.stats.get('committees_created', 0)}, "
            f"Users found: {self.stats['users_found']}, "
            f"Users not found: {self.stats['users_not_found']}, "
            f"Memberships created: {self.stats['memberships_created']}, "
            f"Memberships skipped: {self.stats['memberships_skipped']}, "
            f"Errors: {self.stats['errors']}"
        )
