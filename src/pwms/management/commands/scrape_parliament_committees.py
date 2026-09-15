import logging
import re
import time

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from pwms.models import Group, GroupMembership, Role, User
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

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
            (
                "Committee Content Advisor",
                "Content advisor to a parliamentary committee",
            ),
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
        # Get committee URLs from the committees table
        committees_list = self.scrape_committees_table()

        self.stdout.write(f"Found {len(committees_list)} committees to process")
        logger.info(f"Found {len(committees_list)} committees to process")

        # Process each committee
        for committee_info in committees_list:
            try:
                self.process_committee_from_url(committee_info)
                self.stats["committees_processed"] += 1
                time.sleep(1)  # Be respectful to the server
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(
                    f"Error processing committee {committee_info.get('name', 'unknown')}: {e}"
                )

    def scrape_committees_table(self):
        """Scrape committee list from parliament.gov.za using Selenium"""
        url = "https://www.parliament.gov.za/committees?perPage=100"

        driver = None
        try:
            self.stdout.write(f"Fetching committees from: {url}")
            logger.info(f"Fetching committees from: {url}")

            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

            driver = webdriver.Chrome(options=chrome_options)
            driver.get(url)

            self.stdout.write("Waiting for page to load...")
            logger.info("Waiting for page to load...")

            wait = WebDriverWait(driver, 30)
            wait.until(EC.presence_of_element_located((By.ID, "committees-table")))

            self.stdout.write("Waiting for table data to populate...")
            logger.info("Waiting for table data to populate...")
            time.sleep(8)

            committees = []

            # Try to extract data using JavaScript first
            try:
                # Execute JavaScript to get the table data from the Vue/React component
                table_data = driver.execute_script("""
                    var rows = [];
                    var tableRows = document.querySelectorAll('#committees-table tbody tr');
                    tableRows.forEach(function(row) {
                        var cells = row.querySelectorAll('td');
                        if (cells.length >= 2) {
                            var link = cells[0].querySelector('a');
                            var rawName = link ? link.textContent.trim() : cells[0].textContent.trim();
                            // Clean up extra spaces in committee name
                            var name = rawName.replace(/\\s+/g, ' ').trim();
                            var house = cells[1].textContent.trim();

                            // Try to get URL/ID from various sources
                            var url = null;
                            var committeeId = null;

                            // Check row onclick first (most reliable)
                            var rowOnclick = row.getAttribute('onclick');
                            if (rowOnclick && rowOnclick.includes('window.location.replace')) {
                                var match = rowOnclick.match(/window\\.location\\.replace\\('([^']+)'\\)/);
                                if (match) {
                                    url = match[1];
                                }
                            }

                            // Fallback to link attributes if row onclick didn't work
                            if (!url && link) {
                                // Check href first
                                url = link.getAttribute('href');

                                // If href is a hash route like #/committee-details/123
                                if (url && url.includes('#/committee-details/')) {
                                    var match = url.match(/#\\/committee-details\\/(\\d+)/);
                                    if (match) {
                                        committeeId = match[1];
                                    }
                                }

                                // Try to extract from href even if it's just /committee-details/123
                                if (!committeeId && url && url.includes('committee-details/')) {
                                    var match = url.match(/committee-details\\/(\\d+)/);
                                    if (match) {
                                        committeeId = match[1];
                                    }
                                }

                                // Try Vue router-link 'to' attribute
                                if (!committeeId) {
                                    var to = link.getAttribute('to') || link.getAttribute(':to') || link.getAttribute('v-bind:to');
                                    if (to && to.includes('committee-details')) {
                                        var match = to.match(/committee-details['\\/\"]*(\\d+)/);
                                        if (match) {
                                            committeeId = match[1];
                                        }
                                    }
                                }

                                // Try all data attributes
                                if (!committeeId) {
                                    var attrs = link.attributes;
                                    for (var i = 0; i < attrs.length; i++) {
                                        var attr = attrs[i];
                                        if (attr.value && attr.value.toString().match(/^\\d+$/)) {
                                            committeeId = attr.value;
                                            break;
                                        }
                                    }
                                }
                            }

                            // If we found an ID, construct the URL
                            if (committeeId) {
                                url = '/committee-details/' + committeeId;
                            }

                            rows.push({name: name, house: house, url: url, id: committeeId});
                        }
                    });
                    return rows;
                """)

                if table_data and len(table_data) > 0:
                    self.stdout.write(
                        f"Extracted {len(table_data)} committees via JavaScript"
                    )
                    logger.info(
                        f"Extracted {len(table_data)} committees via JavaScript"
                    )

                    for idx, data in enumerate(table_data):
                        committee_name = data.get("name", "").strip()
                        parent_group = data.get("house", "").strip()
                        committee_url = data.get("url")

                        # Normalize NCOP to full name
                        if parent_group == "NCOP":
                            parent_group = "National Council of Provinces"

                        # Construct full URL if we have a relative path
                        if committee_url and committee_url.startswith("/"):
                            committee_url = (
                                f"https://www.parliament.gov.za{committee_url}"
                            )

                        if committee_name and parent_group and committee_url:
                            committees.append(
                                {
                                    "name": committee_name,
                                    "url": committee_url,
                                    "parent_group": parent_group,
                                }
                            )

                            if self.verbose:
                                self.stdout.write(
                                    f"Found: {committee_name} (Parent: {parent_group}, URL: {committee_url})"
                                )

                    return committees
            except Exception as e:
                logger.warning(
                    f"JavaScript extraction failed: {e}, falling back to Selenium element extraction"
                )

            # Fallback to element-by-element extraction
            rows = driver.find_elements(By.CSS_SELECTOR, "#committees-table tbody tr")
            self.stdout.write(f"Found {len(rows)} table rows")
            logger.info(f"Found {len(rows)} table rows")

            for idx, row in enumerate(rows):
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) >= 2:
                        name_cell = cells[0]
                        house_cell = cells[1]

                        committee_url = None
                        committee_name = None

                        # Try to get committee name and URL from link
                        try:
                            link = name_cell.find_element(By.TAG_NAME, "a")
                            committee_name = link.text.strip()

                            # Debug first row
                            if idx == 0:
                                href = link.get_attribute("href")
                                onclick = link.get_attribute("onclick")
                                row_onclick = row.get_attribute("onclick")
                                logger.info(
                                    f"DEBUG Row 0: href='{href}', onclick='{onclick}', row_onclick='{row_onclick}'"
                                )

                            # Try multiple ways to get the URL
                            href = link.get_attribute("href")
                            if href and href != "None" and "committee-details" in href:
                                committee_url = (
                                    href
                                    if href.startswith("http")
                                    else f"https://www.parliament.gov.za{href}"
                                )
                            else:
                                # Try onclick attribute
                                onclick = link.get_attribute("onclick")
                                if onclick and "committee-details" in onclick:
                                    import re

                                    match = re.search(
                                        r"committee-details/(\d+)", onclick
                                    )
                                    if match:
                                        committee_url = f"https://www.parliament.gov.za/committee-details/{match.group(1)}"
                                else:
                                    # Try data attributes
                                    data_id = link.get_attribute("data-id")
                                    if data_id:
                                        committee_url = f"https://www.parliament.gov.za/committee-details/{data_id}"
                                    else:
                                        # Try to get from row onclick
                                        row_onclick = row.get_attribute("onclick")
                                        if (
                                            row_onclick
                                            and "committee-details" in row_onclick
                                        ):
                                            match = re.search(
                                                r"committee-details/(\d+)", row_onclick
                                            )
                                            if match:
                                                committee_url = f"https://www.parliament.gov.za/committee-details/{match.group(1)}"
                        except Exception:
                            committee_name = name_cell.text.strip()

                        parent_group = house_cell.text.strip()

                        # Normalize NCOP to full name
                        if parent_group == "NCOP":
                            parent_group = "National Council of Provinces"

                        if committee_name and parent_group and committee_url:
                            committees.append(
                                {
                                    "name": committee_name,
                                    "url": committee_url,
                                    "parent_group": parent_group,
                                }
                            )

                            if self.verbose:
                                self.stdout.write(
                                    f"Found: {committee_name} (Parent: {parent_group}, URL: {committee_url})"
                                )
                except Exception as e:
                    logger.debug(f"Skipping row: {e}")
                    continue

            return committees

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error fetching committees: {e}"))
            logger.error(f"Error fetching committees: {e}")
            return []
        finally:
            if driver:
                driver.quit()

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
                            committee_name = re.sub(" +", " ", name).strip()
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
                # Handle members with or without URLs
                member_url = member_data.get("url") or ""
                user = self.find_existing_user_only(member_data["name"], member_url)
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

    def process_committee_from_url(self, committee_info):
        """Process committee using URL from committees table"""
        committee_name = committee_info["name"]
        committee_url = committee_info["url"]
        parent_group_name = committee_info["parent_group"]

        if self.verbose:
            self.stdout.write(
                f"Processing: {committee_name} (Parent: {parent_group_name})"
            )

        # Find or create committee group
        committee_group = self.find_or_create_committee_group(
            committee_name, parent_group_name
        )
        if not committee_group:
            self.stats["missing_groups"].append(committee_name)
            logger.warning(
                f"Could not find or create committee group: {committee_name}"
            )
            return

        # Get committee members from the committee detail page
        committee_members = self.scrape_committee_members(committee_url)

        # Process committee members
        for member_data in committee_members:
            member_name = member_data["name"]
            member_url = member_data.get("url") or ""
            member_email = member_data.get("email")  # Get email if available
            role = member_data.get("role", "Committee Member")
            section = member_data.get("section", "general")

            if self.verbose and section == "contact-details":
                self.stdout.write(
                    f"  Processing non-linked member: {member_name} as {role} (email: {member_email})"
                )

            # Try to find existing user first
            user = self.find_existing_user_only(member_name, member_url, member_email)

            if not user and not member_url:
                # This is a non-linked member, create placeholder user
                if self.verbose:
                    self.stdout.write(f"  Creating placeholder user for: {member_name}")
                user = self.create_placeholder_user(member_name, role)
                if user:
                    if "placeholder_users_created" not in self.stats:
                        self.stats["placeholder_users_created"] = 0
                    self.stats["placeholder_users_created"] += 1
                else:
                    logger.warning(
                        f"Failed to create placeholder user for: {member_name}"
                    )

            if user:
                if self.verbose:
                    self.stdout.write(
                        f"  Adding {member_name} as {role} (from {section} section)"
                    )

                self.create_membership(user, committee_group, role)
            else:
                # Skip membership creation if user not found and couldn't create placeholder
                self.stats["memberships_skipped"] += 1
                if self.verbose and not member_url:
                    self.stdout.write(
                        f"  Skipped {member_name} - could not create placeholder user"
                    )

    def find_or_create_committee_group(self, committee_name, parent_group_name):
        """Find or create committee group with parent"""
        if self.dry_run:
            return True  # Assume it exists for dry run

        # Try exact match first
        group = Group.objects.filter(name__iexact=committee_name).first()
        if group:
            return self._ensure_committee_under_umbrella(group, parent_group_name)

        # Try partial matches
        for group in Group.objects.filter(name__icontains="committee"):
            if self.names_similar(group.name, committee_name):
                logger.info(
                    f'Matched "{committee_name}" to existing group "{group.name}"'
                )
                return self._ensure_committee_under_umbrella(group, parent_group_name)

        # If no match found, create the missing committee with parent
        logger.info(f"Committee not found in database, creating: {committee_name}")
        return self.create_missing_committee_with_parent(
            committee_name, parent_group_name
        )

    def create_missing_committee_with_parent(self, committee_name, parent_group_name):
        """Create a missing committee group with its parent house"""
        try:
            # Determine committee type based on name
            committee_type = self.determine_committee_type(committee_name)

            # Generate short name (max 50 chars)
            short_name = committee_name
            if len(short_name) > 20:
                # Try to abbreviate common words
                abbreviations = {
                    "Portfolio Committee": "PC",
                    "Select Committee": "SC",
                    "Standing Committee": "STDC",
                    "Joint Committee": "JC",
                    "Special Committee": "SPC",
                    "Ad Hoc Committee": "AHOC",
                    "Joint Standing Committee": "JSC",
                    "and": "&",
                    "Development": "Dev",
                    "International": "Intl",
                    "Administration": "Admin",
                    "Constitutional": "Const",
                }

                for full_word, abbrev in abbreviations.items():
                    short_name = short_name.replace(full_word, abbrev)

                # If still too long, truncate
                if len(short_name) > 20:
                    short_name = short_name[:17] + "..."

            # Resolve the umbrella committee group (e.g. "NA Committees" /
            # "NCOP Committees" / "Joint Committees") this committee belongs to.
            parent_group = self._get_or_create_committee_umbrella(parent_group_name)

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
                f"Created missing committee: {committee_name} "
                f"(type: {committee_type}, parent: {parent_group.name})"
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

        # First check for exact match after normalization
        if name1_clean == name2_clean:
            return True

        # Remove common words
        common_words = {"committee", "on", "and", "the", "of", "for", "in"}
        name1_words = set(name1_clean.split()) - common_words
        name2_words = set(name2_clean.split()) - common_words

        # Check overlap
        if len(name1_words) == 0 or len(name2_words) == 0:
            return False

        # Calculate overlap
        overlap = len(name1_words & name2_words)

        # For a match, require:
        # 1. High similarity (80% or more), OR
        # 2. One set is a complete subset of the other (all words match)
        similarity = overlap / max(len(name1_words), len(name2_words))
        is_subset = (name1_words <= name2_words) or (name2_words <= name1_words)

        return similarity >= 0.8 or is_subset

    def create_missing_committee(self, committee_name):
        """Create a missing committee group based on scraped data"""
        try:
            # Determine committee type based on name
            committee_type = self.determine_committee_type(committee_name)

            # Generate short name (max 50 chars)
            short_name = committee_name
            if len(short_name) > 20:
                # Try to abbreviate common words
                abbreviations = {
                    # "Committee": "Comm",
                    "Portfolio Committee": "PC",
                    "Select Committee": "SC",
                    "Standing Committee": "STDC",
                    "Joint Committee": "JC",
                    "Special Committee": "SPC",
                    "Ad Hoc Committee": "AHOC",
                    "Joint Standing Committee": "JSC",
                    "and": "&",
                    "Development": "Dev",
                    "International": "Intl",
                    "Administration": "Admin",
                    "Constitutional": "Const",
                }

                for full_word, abbrev in abbreviations.items():
                    short_name = short_name.replace(full_word, abbrev)

                # If still too long, truncate
                if len(short_name) > 20:
                    short_name = short_name[:17] + "..."

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
        """Map a committee name to a valid ``Group.group_type`` choice."""
        name_lower = committee_name.lower()

        if "portfolio committee" in name_lower:
            return "portfolio_committee"
        if "select committee" in name_lower:
            return "select_committee"
        if "public accounts" in name_lower:
            return "public_accounts_committee"
        if "ad hoc" in name_lower:
            return "ad_hoc_committee"
        if "joint" in name_lower:
            return "joint_committee"
        if (
            "special" in name_lower
            or "multi party" in name_lower
            or "multi-party" in name_lower
            or "caucus" in name_lower
        ):
            return "special_committee"
        if "internal" in name_lower:
            return "internal_committee"
        # Standing committees, subcommittees, constitutional review, etc. have
        # no dedicated model choice, so fall back to the generic type.
        return "committee"

    def _normalize_house_name(self, house_name: str) -> str:
        """Map the scraped house label to the canonical house name."""
        name = (house_name or "").strip()
        aliases = {
            "NA": "National Assembly",
            "NCOP": "National Council of Provinces",
            "JOINT": "Joint Sitting",
            "Joint": "Joint Sitting",
        }
        return aliases.get(name, name)

    def _get_parliament_root(self):
        return Group.objects.filter(name="Parliament", parent__isnull=True).first()

    def _get_or_create_house(self, house_name: str):
        house_name = self._normalize_house_name(house_name)
        house = Group.objects.filter(name=house_name, group_type="house").first()
        if house is not None:
            return house
        parliament = self._get_parliament_root()
        return Group.objects.create(
            name=house_name,
            short_name=house_name[:50],
            group_type="house",
            description=f"{house_name} parliamentary house",
            is_active=True,
            parent=parliament,
            start_date=timezone.now().date(),
        )

    def _get_or_create_committee_umbrella(self, house_name: str) -> Group:
        """Return the umbrella committee group a committee belongs under.

        Committees are stored under "NA Committees" / "NCOP Committees" (or a
        single "Joint Committees" umbrella under Parliament), not directly under
        the House.
        """
        house_name = self._normalize_house_name(house_name)

        if house_name == "National Council of Provinces":
            umbrella_name = "NCOP Committees"
            parent = self._get_or_create_house(house_name)
        elif house_name == "Joint Sitting":
            umbrella_name = "Joint Committees"
            parent = self._get_parliament_root()
        else:  # National Assembly and anything unclassified
            umbrella_name = "NA Committees"
            parent = self._get_or_create_house("National Assembly")

        umbrella = Group.objects.filter(name=umbrella_name, parent=parent).first()
        if umbrella is None:
            umbrella = Group.objects.create(
                name=umbrella_name,
                short_name=umbrella_name[:50],
                group_type="committee",
                description=f"Umbrella group for {umbrella_name}",
                is_active=True,
                parent=parent,
            )
        return umbrella

    def _ensure_committee_under_umbrella(self, group, house_name):
        """Move an existing committee into its umbrella if it sits under a house.

        Committees scraped by older versions of this command were parented
        directly under the House; re-parent them to "NA Committees" /
        "NCOP Committees" / "Joint Committees" so the tree stays consistent.
        """
        if group.parent is None or group.parent.group_type != "house":
            return group
        if group.group_type not in {
            "committee",
            "portfolio_committee",
            "select_committee",
            "special_committee",
            "public_accounts_committee",
            "internal_committee",
            "ad_hoc_committee",
            "joint_committee",
        }:
            return group

        umbrella = self._get_or_create_committee_umbrella(house_name)
        if group.parent_id == umbrella.id:
            return group

        old_parent = group.parent.name
        group.parent = umbrella
        group.save(update_fields=["parent"])
        logger.info(
            f"Re-parented committee '{group.name}' from '{old_parent}' to '{umbrella.name}'"
        )
        return group

    def get_or_create_parent_group(self, committee_name, committee_type):
        """Backward-compatible wrapper: parent is the committee umbrella."""
        name_lower = committee_name.lower()
        if committee_type == "joint_committee" or "joint" in name_lower:
            return self._get_or_create_committee_umbrella("Joint Sitting")
        if committee_type == "select_committee" or "select committee" in name_lower:
            return self._get_or_create_committee_umbrella(
                "National Council of Provinces"
            )
        return self._get_or_create_committee_umbrella("National Assembly")

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

                # Extract non-linked members (like secretaries in <b> tags)
                non_linked_members = self.extract_non_linked_members(soup)
                members.extend(non_linked_members)

                # If no structured sections found, fall back to general member extraction
                if not composition_members and not alternate_members:
                    linked_members = self.extract_members_general(soup)
                    members.extend(linked_members)
                    logger.info(
                        "No structured sections found, using general extraction"
                    )
                else:
                    logger.info(
                        f"Found {len(composition_members)} composition members and {len(alternate_members)} alternate members"
                    )

                if non_linked_members:
                    logger.info(
                        f"Found {len(non_linked_members)} non-linked members (secretaries, etc.)"
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

        # First check the immediate parent's text (including siblings)
        # This catches patterns like: <a>Name</a> (Chairperson)
        parent = link.parent
        if parent:
            parent_full_text = parent.get_text(strip=True).lower()

            # Check for role in parentheses or after the link
            if re.search(r"\(chairperson\)", parent_full_text):
                role = "Committee Chairperson"
            elif re.search(r"\(deputy\s+chairperson\)", parent_full_text):
                role = "Committee Deputy Chairperson"
            elif re.search(r"\(secretary\)", parent_full_text):
                role = "Committee Secretary"
            # Check for role labels with colons
            elif re.search(r"chairperson\s*:", parent_full_text):
                role = "Committee Chairperson"
            elif re.search(r"deputy\s+chair", parent_full_text):
                role = "Committee Deputy Chairperson"
            elif re.search(r"secretary\s*:", parent_full_text) or (
                re.search(r"\bcommittee\s+secretary\b", parent_full_text)
                and len(parent_full_text) < 150
            ):
                role = "Committee Secretary"

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

    def extract_non_linked_members(self, soup):
        """Extract members without person-details links (like secretaries, content advisors, researchers from Contact details section)"""
        members = []
        seen_names = set()

        # Look for Contact details section specifically
        # Use a lambda function to search for headings containing "contact" and "details"
        def is_contact_details_heading(tag):
            if tag.name in ["h4", "h5", "h6"]:
                text = tag.get_text(strip=True).lower()
                return "contact" in text and "details" in text
            return False

        contact_heading = soup.find(is_contact_details_heading)
        contact_section = None

        if contact_heading:
            logger.info("Found Contact details heading")
            # Find the list items in the Contact details section
            # There might be a <p> tag between the heading and the list
            # We need to find the next <ul> but make sure it's before the next heading
            next_heading = contact_heading.find_next_sibling(["h4", "h5", "h6"])
            contact_section = None

            # Find all ul tags after the contact heading
            for ul in contact_heading.find_all_next("ul"):
                # Check if this ul comes before the next heading
                if next_heading and ul.sourceline and next_heading.sourceline:
                    if ul.sourceline < next_heading.sourceline:
                        contact_section = ul
                        break
                elif not next_heading:
                    # No next heading, so this is the contact section
                    contact_section = ul
                    break

            # Fallback: just get the next ul if we couldn't determine by position
            if not contact_section:
                contact_section = contact_heading.find_next("ul")

            if contact_section:
                list_items = contact_section.find_all("li")
                logger.info(f"Found {len(list_items)} contact details items")
            else:
                logger.info("Contact details heading found but no list section")
        else:
            if self.verbose:
                self.stdout.write("  No Contact details section found")

        # Process the list items if we found them
        if contact_section:
            for li in list_items:
                # Extract name from <b> tag
                bold_tag = li.find("b")
                if not bold_tag:
                    continue

                name_text = bold_tag.get_text(strip=True)

                logger.info(f"Processing contact item: '{name_text}'")

                # Skip if too short or already seen
                if len(name_text) < 5 or name_text in seen_names:
                    logger.info(f"Skipping '{name_text}': too short or already seen")
                    continue

                # Check if this looks like a person name (has at least 2 words, starts with capital)
                # Strip leading/trailing whitespace first
                name_text = name_text.strip()
                if not re.match(r"^[A-Z][a-z]+\s+[A-Z]", name_text):
                    logger.info(f"Skipping '{name_text}': doesn't match name pattern")
                    continue

                # Get the full text of the list item to determine role
                # Use separator=' ' to add spaces between elements (including <br> tags)
                li_text = li.get_text(separator=" ", strip=True).lower()

                # Extract email address if available
                email = None
                mailto_link = li.find("a", href=re.compile(r"^mailto:"))
                if mailto_link:
                    email = mailto_link.get("href", "").replace("mailto:", "").strip()

                # Check for various role types
                role = None
                if re.search(r"\bcommittee\s+secretary\b", li_text) or re.search(
                    r"\bsecretary\b", li_text
                ):
                    role = "Committee Secretary"
                elif re.search(r"\bdeputy\s+chairperson\b", li_text):
                    role = "Committee Deputy Chairperson"
                elif re.search(r"\bchairperson\b", li_text):
                    role = "Committee Chairperson"
                elif re.search(r"\bcontent\s+advisor\b", li_text):
                    role = "Committee Content Advisor"
                elif re.search(r"\badvisor\b", li_text):
                    role = "Committee Advisor"
                elif re.search(r"\bresearcher\b", li_text):
                    role = "Committee Researcher"

                if not role:
                    # Skip if no clear role indicator
                    logger.info(
                        f"Skipping '{name_text}': no role indicator found in '{li_text[:100]}'"
                    )
                    continue

                seen_names.add(name_text)
                members.append(
                    {
                        "name": name_text,
                        "url": None,  # No URL for non-linked members
                        "email": email,  # Include email if found
                        "role": role,
                        "section": "contact-details",
                    }
                )
                if self.verbose:
                    self.stdout.write(
                        f"    Added: {name_text} as {role} (email: {email})"
                    )

        return members

        #     seen_urls = set()
        #     for link in member_links:
        #         member_url = link.get("href")
        #         if member_url not in seen_urls:
        #             seen_urls.add(member_url)
        #             member = self.extract_member_from_link(
        #                 link, "Committee Member", "general"
        #             )
        #             if member:
        #                 members.append(member)

        return members

    def scrape_username_from_profile(self, profile_url):
        """Scrape username from member's profile page by extracting email address"""
        if not profile_url:
            return None, None

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            with httpx.Client(timeout=30.0) as client:
                response = client.get(profile_url, headers=headers)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "html.parser")

                # Look for email addresses in various formats
                username = None

                # Method 1: Look for mailto links
                mailto_links = soup.find_all("a", href=re.compile(r"^mailto:"))
                personal_emails = []
                generic_emails = []

                for link in mailto_links:
                    email_raw = link.get("href", "").replace("mailto:", "").strip()

                    # Handle multiple emails in mailto (separated by /, ;, or space)
                    possible_emails = []

                    # Split by common separators
                    for separator in [" / ", "/", ";", " ", ","]:
                        if separator in email_raw:
                            possible_emails.extend(
                                [
                                    email.strip()
                                    for email in email_raw.split(separator)
                                    if email.strip()
                                ]
                            )
                            break
                    else:
                        # No separator found, use the whole string
                        possible_emails.append(email_raw)

                    # Try each email found
                    for email in possible_emails:
                        username = self.extract_username_from_email(email)
                        if username:
                            # Check if it's a personal email or generic
                            if (
                                email.lower().startswith("info@")
                                or email.lower().startswith("contact@")
                                or email.lower().startswith("admin@")
                            ):
                                generic_emails.append((email, username))
                            else:
                                personal_emails.append((email, username))

                # Prioritize personal emails over generic ones
                all_emails = personal_emails + generic_emails

                for email, username in all_emails:
                    logger.info(
                        f"Found username {username} from mailto link for {profile_url}"
                    )
                    return username, email

                # Method 2: Look for email text patterns
                email_patterns = [
                    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                    r"Email:\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})",
                    r"E-mail:\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})",
                ]

                page_text = soup.get_text()
                for pattern in email_patterns:
                    matches = re.findall(pattern, page_text, re.IGNORECASE)
                    for match in matches:
                        email = (
                            match
                            if isinstance(match, str)
                            else match[0]
                            if len(match) > 0
                            else ""
                        )
                        extracted_username = self.extract_username_from_email(email)
                        if extracted_username:
                            logger.info(
                                f"Found username {extracted_username} from email pattern for {profile_url}"
                            )
                            return extracted_username, email

                # Method 3: Look for specific parliament email format elements
                # Parliament emails often follow specific patterns
                email_elements = soup.find_all(
                    string=re.compile(r"@parliament\.gov\.za", re.IGNORECASE)
                )
                for element in email_elements:
                    email_match = re.search(
                        r"([A-Za-z0-9._%+-]+@parliament\.gov\.za)",
                        element,
                        re.IGNORECASE,
                    )
                    if email_match:
                        email = email_match.group(1)
                        username = self.extract_username_from_email(email)
                        if username:
                            logger.info(
                                f"Found username {username} from parliament email for {profile_url}"
                            )
                            return username, email

                logger.warning(f"No email found for profile: {profile_url}")
                return None, None

        except httpx.HTTPError as e:
            logger.error(f"Error fetching profile {profile_url}: {e}")
            return None, None
        except Exception as e:
            logger.error(f"Error parsing profile {profile_url}: {e}")
            return None, None

    def extract_username_from_email(self, email):
        """Extract username from email address"""
        if not email:
            return None

        # Clean email and extract username part
        email = email.strip().lower()

        # Basic email validation
        email_pattern = r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$"
        if not re.match(email_pattern, email):
            return None

        # Extract username part before @
        username = email.split("@")[0]

        # Remove common separators and normalize
        username = username.replace(".", "").replace("_", "").replace("-", "")

        # Ensure it's a reasonable username length
        if len(username) < 3 or len(username) > 20:
            return None

        return username

    def find_existing_user_only(self, full_name, profile_url, email=None):
        """Find existing user only - do not create new users"""
        name_parts = self.parse_full_name(full_name)

        if not self.dry_run:
            # Strategy 0: Check provided email first (for non-linked members)
            if email:
                user = User.objects.filter(email__iexact=email).first()
                if user:
                    self.stats["users_found"] += 1
                    if self.verbose:
                        self.stdout.write(
                            f"Found existing user by provided email: {user.username} for {full_name}"
                        )
                    logger.info(
                        f"Found user {user.username} via email {email} for {full_name}"
                    )
                    return user

            # Strategy 1: Try to get username and email from profile page first
            profile_username = None
            profile_email = None
            if profile_url:
                profile_username, profile_email = self.scrape_username_from_profile(
                    profile_url
                )

                if profile_email:
                    # Strategy 1a: Try to find user by exact email match
                    user = User.objects.filter(email__iexact=profile_email).first()
                    if user:
                        self.stats["users_found"] += 1
                        if self.verbose:
                            self.stdout.write(
                                f"Found existing user by email: {user.username} for {full_name}"
                            )
                        logger.info(
                            f"Found user {user.username} via email {profile_email} for {full_name}"
                        )
                        return user

                if profile_username:
                    # Strategy 0b: Try to find user by scraped username
                    user = User.objects.filter(
                        username__iexact=profile_username
                    ).first()
                    if user:
                        self.stats["users_found"] += 1
                        if self.verbose:
                            self.stdout.write(
                                f"Found existing user by profile username: {user.username} for {full_name}"
                            )
                        logger.info(
                            f"Found user {user.username} via profile username for {full_name}"
                        )
                        return user

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
                # For non-linked members (no profile_url), try more aggressive name matching
                if not profile_url:
                    user = self.find_existing_user_aggressive(name_parts, full_name)
                    if user:
                        self.stats["users_found"] += 1
                        if self.verbose:
                            self.stdout.write(
                                f"Found existing user by aggressive search: {user.username} for {full_name}"
                            )
                        logger.info(
                            f"Found user {user.username} via aggressive name search for {full_name}"
                        )
                        return user

                # Log user not found
                self.stats["users_not_found"] += 1
                self.stats["missing_users"].append(
                    {
                        "full_name": full_name,
                        "profile_url": profile_url,
                        "parsed_name": name_parts,
                        "search_key": search_key,
                        "profile_username": profile_username,
                        "profile_email": profile_email,
                    }
                )
                logger.warning(
                    f"User not found in system: {full_name} (search key: {search_key}, profile username: {profile_username}, profile email: {profile_email})"
                )
                if self.verbose:
                    self.stdout.write(f"User not found: {full_name}")
                return None
        else:
            self.stdout.write(f"[DRY RUN] Would search for user: {full_name}")
            return None

    def find_existing_user_aggressive(self, name_parts, full_name):
        """More aggressive user search for non-linked members"""
        first_name = name_parts["first_name"]
        last_name = name_parts["last_name"]

        if not first_name or not last_name:
            return None

        # Strategy 1: Exact match (case insensitive)
        user = User.objects.filter(
            first_name__iexact=first_name, last_name__iexact=last_name
        ).first()
        if user:
            return user

        # Strategy 2: First name exact, last name contains
        user = User.objects.filter(
            first_name__iexact=first_name, last_name__icontains=last_name
        ).first()
        if user:
            return user

        # Strategy 3: Last name exact, first name contains
        user = User.objects.filter(
            first_name__icontains=first_name, last_name__iexact=last_name
        ).first()
        if user:
            return user

        # Strategy 4: Both names contain
        user = User.objects.filter(
            first_name__icontains=first_name, last_name__icontains=last_name
        ).first()
        if user:
            return user

        # Strategy 5: Try with just first word of last name (for hyphenated names)
        last_name_first_word = last_name.split()[0] if " " in last_name else last_name
        user = User.objects.filter(
            first_name__iexact=first_name, last_name__iexact=last_name_first_word
        ).first()
        if user:
            return user

        # Strategy 6: Try with just last word of last name
        last_name_last_word = last_name.split()[-1] if " " in last_name else last_name
        user = User.objects.filter(
            first_name__iexact=first_name, last_name__iexact=last_name_last_word
        ).first()
        if user:
            return user

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
                last_name = parts[-1]
                first_name = " ".join(parts[:-1])
            elif len(parts) == 1:
                first_name = parts[0]

        return {"title": title, "first_name": first_name, "last_name": last_name}

    def create_placeholder_user(self, full_name, role_name):
        """Create a placeholder user for non-linked members"""
        if self.dry_run:
            return None

        try:
            name_parts = self.parse_full_name(full_name)
            first_name = name_parts["first_name"]
            last_name = name_parts["last_name"]

            # Generate username based on name pattern
            username = self.generate_placeholder_username(first_name, last_name)

            # Create placeholder user
            user = User.objects.create(
                username=username,
                first_name=first_name,
                last_name=last_name,
                email=f"{username}@placeholder.parliament.gov.za",
                is_active=False,  # Mark as inactive placeholder
                is_staff=False,
                is_superuser=False,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Created placeholder user: {username} for {full_name}"
                )
            )
            logger.info(
                f"Created placeholder user: {username} for {full_name} as {role_name}"
            )

            return user

        except Exception as e:
            logger.error(f"Error creating placeholder user for {full_name}: {e}")
            return None

    def generate_placeholder_username(self, first_name, last_name):
        """Generate a unique placeholder username"""
        if not first_name or not last_name:
            # Fallback to generic username
            return f"placeholder_{User.objects.count() + 1}"

        # Generate base username using same pattern as existing users
        base_username = self.generate_user_search_key(first_name, last_name)

        # Ensure uniqueness
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        return username

    @transaction.atomic
    def create_membership(self, user, group, role_name):
        """Create or reactivate a membership keyed by (user, group, role)."""
        if self.dry_run:
            self.stdout.write(
                f"[DRY RUN] Would create membership: {user} -> {group} as {role_name}"
            )
            return

        try:
            role = Role.objects.get(name=role_name)
        except Role.DoesNotExist:
            logger.error(f"Role not found: {role_name}")
            self.stdout.write(self.style.ERROR(f"Role not found: {role_name}"))
            return

        membership = GroupMembership.objects.filter(
            user=user, group=group, role=role
        ).first()

        if membership is None:
            GroupMembership.objects.create(
                user=user,
                group=group,
                role=role,
                is_active=True,
                start_date=timezone.now().date(),
            )
            self.stats["memberships_created"] += 1
            if self.verbose:
                self.stdout.write(f"Created membership: {user} -> {group}")
            logger.info(
                f"Created membership: {user.username} -> {group.name} ({role_name})"
            )
        elif not membership.is_active:
            membership.is_active = True
            membership.end_date = None
            membership.save(update_fields=["is_active", "end_date"])
            self.stats["memberships_updated"] += 1
            logger.info(
                f"Reactivated membership: {user.username} -> {group.name} ({role_name})"
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
        self.stdout.write(
            f"Placeholder users created: {self.stats.get('placeholder_users_created', 0)}"
        )
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
            f"Placeholder users created: {self.stats.get('placeholder_users_created', 0)}, "
            f"Memberships created: {self.stats['memberships_created']}, "
            f"Memberships skipped: {self.stats['memberships_skipped']}, "
            f"Errors: {self.stats['errors']}"
        )
