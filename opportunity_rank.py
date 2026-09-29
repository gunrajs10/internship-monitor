"""Explainable, offline triage for Gunraj's US healthcare opportunities.

This module makes no network calls and never equates a keyword match with proven
eligibility. Unknown requirements are preserved as cautions for review.
"""

import html
import re


STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
               "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|"
               "maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
               "nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
               "north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
               "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
               "wisconsin|wyoming|district of columbia|puerto rico")
CA_CITIES = ("san francisco|south san francisco|san diego|los angeles|sacramento|davis|"
             "thousand oaks|foster city|santa monica|el segundo|carlsbad|la jolla|novato|"
             "san rafael|redwood city|menlo park|palo alto|sunnyvale|emeryville|berkeley|"
             "hayward|fremont|alameda|irvine|san jose|san mateo|pleasanton|livermore")
US_CITIES = (CA_CITIES + "|boston|waltham|tarrytown|princeton|basking ridge|rahway|"
             "kenilworth|plainsboro|gaithersburg|rockville|research triangle park|raleigh|"
             "durham|indianapolis|chicago|madison|cincinnati|columbus|ann arbor|minneapolis|"
             "saint louis|st\\.? louis|salt lake city|phoenix|austin|dallas|houston|denver|"
             "boulder|philadelphia|pittsburgh|seattle|bothell|new brunswick|raritan")
FOREIGN = ("canada|canadian|united kingdom|uk|england|scotland|ireland|germany|switzerland|"
           "denmark|sweden|norway|finland|france|spain|italy|netherlands|belgium|austria|"
           "poland|portugal|czechia|czech republic|romania|hungary|greece|turkey|israel|"
           "india|china|japan|singapore|taiwan|south korea|australia|new zealand|mexico|"
           "brazil|argentina|colombia|chile|south africa|nigeria|egypt|uae|saudi arabia|"
           "malaysia|philippines|indonesia|vietnam|pakistan|bangladesh|hong kong|"
           "toronto|vancouver|montreal|mississauga|london|dublin|basel|zurich|copenhagen|"
           "paris|berlin|munich|madrid|barcelona|amsterdam|brussels|tokyo|osaka|seoul|"
           "shanghai|beijing|bangalore|bengaluru|hyderabad|mumbai|sydney|melbourne|emea|apac")
SECTOR = r"\b(biotech(?:nology)?|biopharma(?:ceutical)?|pharma(?:ceutical)?s?|health[ -]?care|life sciences?|medical devices?|medtech|diagnostics?|therapeutics?|neurotech(?:nology)?|digital health|hospitals?|clinical trials?|drug (?:discovery|development)|oncology|neuroscience)\b"
PROGRAM = r"\b(intern(?:ship)?s?|co[ -]?op|summer associate|rotational|rotation|(?:leadership|commercial|finance|operations|MBA|graduate) (?:leadership )?development program|leadership program|graduate (?:program|scheme)|emerging leaders?|future leaders?|early talent|CRDP|CLDP|MLDP|FSLDP|LDP|RDP|CLP)\b"
EARLY = r"\b(entry[ -]?level|early[ -]?career|new grad(?:uate)?s?|recent graduate|junior|jr\.?|associate|analyst|coordinator|assistant|trainee|apprentice|technician)\b|\b(?:scientist|engineer|specialist|technologist|researcher|chemist|biologist)\s+(?:I|1)\b"
SENIOR = r"\b(director|principal|vice president|VP|chief|head of|global head|distinguished)\b|\bstaff (?:scientist|engineer)\b|\blead (?:scientist|engineer|analyst|manager)\b"
COMMERCIAL = r"\b(commercial|corporate strateg\w*|business strateg\w*|strateg(?:y|ic) (?:analyst|associate|intern|planning)|competitive intelligence|new product planning|portfolio strategy)\b"
SECONDARY = r"\b(marketing|market access|business development|product (?:strategy|management|manager|marketing)|brand|health economics|HEOR|reimbursement|pricing|payer|market research)\b"
ADJACENT = r"\b(operations|supply chain|finance|financial|corporate development|project|program|business analyst|business analytics|patient access|medical affairs)\b"
RESEARCH = r"\b(research|scientist|laboratory|lab|neurobiology|neuroscience|biology|clinical|regulatory|quality)\b"
OUTSIDE_BACKGROUND = (r"\b(engineer(?:ing)?|developer|programmer|devops|software|counsel|attorney|lawyer|paralegal|"
                      r"HVAC|electrician|plumber|carpenter|mechanic|maintenance technician|facilities technician|"
                      r"field service technician|repair technician|inventory control|warehouse|material handler|"
                      r"truck driver|delivery driver|caregiver|care ?giver|home health aide|nursing assistant|"
                      r"recovery coach|peer support|behavioral therapist|behavior technician|therapist|"
                      r"dental assistant|veterinarian|veterinary technician|(?:graphic|marketing|motion) designer|lab waste technician)\b")
FIELD_SALES = (r"\b(BDR|SDR|sales development representative|business development representative|"
               r"account executive|territory (?:sales|account|manager)|district sales|inside sales|"
               r"field sales|sales (?:representative|rep|specialist|consultant|associate|executive))\b")


def _has(pattern, text):
    return bool(re.search(pattern, text, re.I))


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if v is not None)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def _clauses(description):
    # Retain list boundaries before stripping HTML; preferred clauses must not
    # be confused with mandatory qualifications in a neighboring bullet.
    raw = re.sub(r"</?(?:li|p|div|br|h[1-6])\b[^>]*>", "\n", str(description or ""), flags=re.I)
    return [_text(x) for x in re.split(r"[\n;]|(?<=[.!?])\s+", raw) if _text(x)]


def _preferred(clause):
    return _has(r"\b(preferred|desirable|nice to have|a plus|not required|optional)\b", clause) and not _has(r"\b(must|mandatory|minimum|at least)\b", clause)


def _location(location, description):
    loc = _text(location)
    explicit_us = _has(r"\b(united states(?: of america)?|USA)\b|\bU\.S\.(?:A\.)?|(?:^|[\s,;/()–-])US(?:$|[\s,;/()–-])", loc)
    state_code = bool(re.search(r"(?:[,;–-]\s*|^)(?:" + "|".join(STATES) + r")(?:\s*[,;/)]|\s*$)", loc))
    state_name = _has(r"\b(" + STATE_NAMES + r")\b", loc)
    foreign = _has(r"\b(" + FOREIGN + r")\b", loc)
    # Public employer feeds sometimes use local-language place names only.
    # Recognize explicit foreign countries/Japanese administrative locations;
    # leave genuinely ambiguous remote locations available for manual review.
    localized_foreign = bool(re.search(r"日本|中国|中國|대한민국|한국|Россия|भारत|千葉|東京都|大阪|京都|北海道|神奈川|[一-龯]{1,8}(?:県|府|市|区|町|村)", loc))
    if localized_foreign and not explicit_us and not state_code and not state_name:
        return "foreign", False
    # 'Georgia' is also a country; do not claim it is a US state without context.
    if loc.strip().lower() == "georgia":
        state_name = False
    # A city/state pair disambiguates London, OH or Paris, TX; a foreign country
    # plus 'CA' means Canada rather than California.
    explicit_foreign_country = _has(r"\b(canada|united kingdom|india|australia|france|germany|china|japan)\b", loc)
    if _has(r"\b(toronto|vancouver|montreal|mississauga)\b", loc) and re.search(r"\bCA\s*$", loc) and not explicit_us:
        return "foreign", False
    us = explicit_us or state_code or state_name or (_has(r"\b(" + US_CITIES + r")\b", loc) and not foreign)
    if foreign and not explicit_us and not (state_code and not explicit_foreign_country):
        return "foreign", False
    if loc.upper() == "CA":
        us = False
    if not us and _has(r"\bremote\b", loc):
        if _has(r"\b(?:remote (?:within|in|across|from)|based in|resid(?:e|ing) in|located in) (?:the )?(?:United States|U\.S\.|USA)\b", description):
            us = True
    explicit_ca = _has(r"\bcalifornia\b", loc) or bool(re.search(r"(?:[,;–-]\s*|^)CA(?:\s*[,;/)]|\s*$)", loc))
    other_state = bool(re.search(r"(?:[,;–-]\s*|^)(?:" + "|".join(s for s in STATES if s != "CA") + r")(?:\s*[,;/)]|\s*$)", loc)) or _has(r"\b(" + "|".join(s for s in STATE_NAMES.split("|") if s != "california") + r")\b", loc)
    # Berkeley Heights, NJ and Fremont, NE are not California. Explicit state
    # information takes precedence over a shared city name; multi-state listings
    # that explicitly include California still receive the highlight.
    ca = us and (explicit_ca or (not other_state and _has(r"\b(" + CA_CITIES + r")\b", loc)))
    return ("us" if us else "unknown"), ca


def _salary(job, description):
    supplied = _text(job.get("salary"))
    if supplied:
        return supplied[:300]
    pattern = r"(?:USD\s*|\$)\s*\d[\d,]*(?:\.\d{1,2})?\s*[kK]?(?:\s*(?:-|–|to)\s*\$?\s*\d[\d,]*(?:\.\d{1,2})?\s*[kK]?)?(?:\s*(?:USD|per (?:year|hour|annum)|/(?:year|yr|hour|hr)|annually|annual))?"
    for match in re.finditer(pattern, description, re.I):
        before = description[max(0, match.start() - 150):match.start()]
        # Inspect the current sentence before this amount. A later salary
        # paragraph must not accidentally relabel an earlier wellness benefit.
        before = re.split(r"[.;]\s+", before)[-1]
        after = description[match.end():match.end() + 55]
        perk = r"\b(wellness|allowance|reimbursement|stipend|sign[ -]?on|bonus|relocation|stock|equity|referral|donation|deductible)\b"
        if _has(perk, before[-70:]) or _has(r"^\s*(?:annual\s+)?(?:wellness|allowance|bonus|reimbursement|stipend)\b", after):
            continue
        compensation_cue = _has(r"\b(salary|salaries|base pay|base compensation|pay (?:range|rate|scale)|hourly rate|annual compensation|annual pay|compensation range)\b", before)
        explicit_unit = _has(r"\b(?:per (?:year|hour|annum)|annually|annual)\b|/(?:year|yr|hour|hr)\b", match.group(0))
        if compensation_cue or explicit_unit:
            return match.group(0).strip()
    return ""


def _strong_salary(salary, threshold):
    if not salary or _has(r"\b(hour|hourly|hr|month|monthly|week|weekly|CAD|GBP|EUR|AUD|INR)\b|[£€]", salary):
        return False
    values = []
    for value, kilo in re.findall(r"(?:\$|USD\s*)?(\d[\d,]*(?:\.\d+)?)\s*([kK]?)", salary):
        amount = float(value.replace(",", "")) * (1000 if kilo else 1)
        if amount >= 10000:
            values.append(amount)
    return bool(values and min(values) >= threshold)


def _clinical_title(title):
    # Clinical training programs require a professional credential even when
    # the broad word "program" also matches an adjacent business function.
    if _has(r"\b(?:DVM|VMD|veterinarian)\b", title):
        return True
    if _has(r"\b(?:hospital medicine|advanced practice|physician assistant|nurse practitioner)\b", title) and _has(r"\b(?:fellowship|residency)\b", title):
        return True
    credential_role = _has(r"\b(nurses?|physician|surgeon|pharmacist|dentist|physical therapist|occupational therapist)\b", title)
    # A Physician Partnerships Intern is not necessarily a physician. Required
    # credentials for such business roles are checked in the job description.
    business_role = _has(COMMERCIAL + "|" + SECONDARY + "|" + ADJACENT + r"|\bpartnerships?\b", title)
    return credential_role and not business_role


def _senior_title(title):
    if _has(SENIOR, title):
        return True
    if _has(r"\b(senior|sr\.?)\b", title):
        post_mba_title = _has(r"\b(?:senior|sr\.?)\s+(?:\w+\s+){0,2}(?:associate|analyst)\b", title)
        business_function = _has(COMMERCIAL + "|" + SECONDARY + "|" + ADJACENT, title)
        return not (post_mba_title and business_function)
    return False


def _outside_background(title):
    # Business programs may rotate through manufacturing or technical teams;
    # their program title must still be a business/leadership program.
    business_program = _has(r"\b(?:MBA|operations|commercial|finance|general management) (?:leadership )?(?:development |rotational )?program\b", title)
    if _has(OUTSIDE_BACKGROUND, title) and not business_program:
        return True
    if _has(FIELD_SALES, title):
        return True
    if _has(r"\bsales\b", title):
        strategic_sales = _has(r"\bsales (?:strategy|operations|analytics|enablement|excellence|planning|leadership|development program)\b|\brotational\b", title)
        return not strategic_sales
    return False


def _experience_minimum(clause):
    """One qualification clause's experience floor, excluding study duration.

    Independent experience requirements are cumulative (take the maximum).
    Explicit alternative degree pathways may have different experience floors;
    retain the least demanding pathway without claiming the user qualifies.
    """
    if _preferred(clause):
        return None
    degrees = re.findall(r"\b(?:bachelor(?:'s|s)?|master(?:'s|s)?|MBA|Ph\.?D\.?|B\.?S\.?|M\.?S\.?)\b", clause, re.I)
    alternatives = len(degrees) >= 2 and _has(r"\b(?:or|alternatively)\b", clause)
    matches = list(re.finditer(r"\b(\d{1,2})\s*(?:\+|(?:-|–|to)\s*\d{1,2}|or\s+more|or\s+greater)?\s*(?:years?|yrs?)\b", clause, re.I))
    values = []
    for index, match in enumerate(matches):
        tail = clause[match.end():]
        if _has(r"^\s*(?:[- ]\s*)?(?:(?:college|university|undergraduate|graduate|bachelor(?:'s)?|master(?:'s)?)\s+)?(?:degree|program|course|education|study|studying)\b", tail):
            continue
        before = clause[max(0, match.start() - 45):match.start()]
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
        after = clause[match.end():min(next_start, match.end() + 85)]
        work_phrase = _has(r"^\s*(?:of\s+)?(?:in|within|doing|working|practicing|managing|leading|building|developing|supporting)\b", after)
        # '8+ years in Sales Strategy' is a qualification even without the word
        # experience. Restrict this inference to a qualification-style opening
        # or candidate/requirement context, not 'we have 80 years in healthcare'.
        requirement_context = _has(r"^\s*[-•*]?\s*(?:(?:minimum|at least|over|more than)\s+(?:of\s+)?)?$", clause[:match.start()]) or _has(r"\b(?:must|requires?|required|minimum|candidate|you have|you bring|you possess|at least)\b", before)
        if alternatives or _has(r"\bexperience\b", after) or _has(r"\bexperience\b[^.;]{0,40}$", before) or (work_phrase and requirement_context):
            values.append(int(match.group(1)))
    if not values:
        return None
    return min(values) if alternatives else max(values)


def needs_detail(job: dict, config: dict) -> bool:
    """Cheap discovery triage; sector and precise eligibility await the detail.

    All internships/rotations and plausible early-career titles are candidates,
    alongside high-relevance business titles with an uncertain career level.
    This must run before a detail-request cap so the caller can persist a queue.
    """
    config = config or {}
    title = _text(job.get("title"))
    if not title or _senior_title(title) or _outside_background(title):
        return False
    loc_status, _ = _location(job.get("location"), _text(job.get("description")))
    if loc_status == "foreign" or (loc_status == "unknown" and not config.get("keep_unknown_location", True)):
        return False
    if _clinical_title(title):
        return False
    return _has(PROGRAM + "|" + EARLY + "|" + COMMERCIAL + "|" + SECONDARY + "|" + ADJACENT, title)


def assess(job: dict, config: dict) -> dict:
    """Return repeatable triage; ``include`` means send for review, not qualified.

    Optional config: healthcare_companies (list or mapping), keep_unknown_location
    (true), max_entry_years (3), strong_salary_annual_usd (100000). No personal
    education dates or experience are assumed from these defaults.
    """
    config = config or {}
    title = _text(job.get("title"))
    description = _text(job.get("description"))
    clauses = _clauses(job.get("description"))
    combined = title + " " + description
    program = _has(PROGRAM, title) or _has(r"\b(rotational program|rotations? (?:through|across)|(?:leadership|commercial|finance|operations|MBA|graduate) (?:leadership )?development program|program (?:participants|cohort))\b", description)
    result = {"include": True, "score": 0, "track": "program" if program else "early-stage",
              "priority": "PROMISING", "reasons": [], "cautions": [], "california": False,
              "eligibility": "Check eligibility", "salary_text": _salary(job, description)}
    reasons, cautions = result["reasons"], result["cautions"]

    def reject(reason):
        result["include"] = False
        result["eligibility"] = "Outside scope"
        reasons.append(reason)
        return result

    if not title:
        return reject("Missing job title; cannot assess this posting.")
    loc_status, result["california"] = _location(job.get("location"), description)
    if loc_status == "foreign":
        return reject("Posting location is outside the United States.")
    if loc_status == "unknown":
        cautions.append("US work location is unconfirmed; remote alone does not establish US eligibility.")
        if not config.get("keep_unknown_location", True):
            return reject("US location could not be established.")
    elif result["california"]:
        reasons.append("California location.")
    else:
        reasons.append("US location.")

    if _senior_title(title):
        return reject("Title explicitly indicates a senior role outside the requested career stage.")
    if _outside_background(title):
        return reject("Role is direct sales, a specialized technical/legal/trade profession, or care delivery outside the supported background and target functions.")
    if _has(r"\b(senior|sr\.?)\b", title):
        cautions.append("Senior Associate/Analyst title: closely review the required experience and responsibility level.")
    if _clinical_title(title):
        return reject("Role requires a clinical profession not established by the supplied background.")
    # Some field-sales jobs carry clinical titles. Require direct evidence of
    # account selling or territory sales duties, not merely work with sales teams.
    clinical_sales_title = _has(r"\b(?:clinical account|clinical oncology specialist)\b", title)
    direct_sales_duties = _has(r"\b(?:account sales|uncapped commission)\b", description) or (
        _has(r"\bterritory management\b", description) and
        _has(r"\b(?:sales objectives|performance of sales|sales goals)\b", description))
    if clinical_sales_title and direct_sales_duties:
        return reject("Clinical title describes a field/account-sales role outside the requested strategy and adjacent functions.")
    if _has(r"\b(undergraduate|undergrad|high school)\b", title) and program:
        return reject("Program is explicitly designated for undergraduate or high-school students.")

    if _has(r"\b(?:V\.?I\.?E\.?|iMove)\b", combined) and _has(r"European Economic Area|\bEEA\b", description):
        cautions.append("VIE program: verify EEA citizenship, the stated age limit, and the rule against assignments in your country of citizenship. Nationality and age are unverified; no sponsorship need does not establish eligibility.")

    required_years = []
    credential_section = False
    qualification_section = False
    for clause in clauses:
        if _has(r"^\s*(?:preferred|optional|nice to have)\b", clause):
            credential_section = False
            qualification_section = False
        elif _has(r"^\s*(?:you have|what you bring|qualifications|minimum requirements|required qualifications|requirements)\s*:?\s*$", clause):
            qualification_section = True
        elif _has(r"^\s*(?:licenses?\s*(?:&|and)\s*certifications?|required (?:licenses?|credentials?))\s*:?\s*$", clause):
            credential_section = True
        elif _has(r"^\s*(?:responsibilities|benefits|about us|what we offer|our company)\s*:?\s*$", clause):
            qualification_section = False
            credential_section = False
        if _preferred(clause):
            continue
        mandatory = _has(r"\b(must|required|minimum|at least|only|need(?:ed|s)? to)\b", clause)
        undergrad_completion = _has(r"\bundergraduate students?\s+(?:completing|pursuing|working toward)\b", clause)
        enrollment_required = mandatory or undergrad_completion or _has(r"\bcurrent(?:ly)? (?:enrollment|enrolled)\b", clause)
        if enrollment_required and (undergrad_completion or _has(r"\b(undergraduates? only|undergraduate students? only|must be (?:an? )?(?:current )?undergraduate|current(?:ly)? (?:enrollment|enrolled) in (?:an? )?(?:bachelor|undergraduate)|pursuing (?:an? )?bachelor)", clause)) and not _has(r"\b(or|and/or)\b.{0,60}\b(master|graduate|MBA|advanced degree)", clause):
            return reject("Required undergraduate enrollment conflicts with the MBA-stage search.")
        credentials = r"\b(M\.?D\.?|Pharm\.?\s*D\.?|Doctor of Pharmacy|DVM|VMD|Doctor of Veterinary Medicine|PA-C|physician assistant licen[cs]e|nurse practitioner licen[cs]e|R\.?N\.?|BSN|BScN|registered nurse|CLS|CGMBS|medical licen[cs]e|nursing licen[cs]e|doctoral degree|Ph\.?D\.?)\b"
        listed_credential = credential_section and bool(re.fullmatch(r"\s*[-•*]?\s*(?:RN|BSN|CLS|CGMBS|registered nurse)\s*", clause, re.I))
        explicit_lab_license = _has(r"\b(?:current|active|valid|required|must|hold|possess)\b", clause) and _has(r"\b(?:CLS|CGMBS)\b", clause) and _has(r"\blicen[cs]e\b", clause)
        if explicit_lab_license:
            return reject("A current clinical laboratory license is required and is not established by the supplied background.")
        active_clinical_license = _has(r"\b(?:current|valid|active|unrestricted)\b", clause) and _has(r"\blicen[cs]e\b", clause) and _has(r"\b(?:nursing|medical|pharmacy|pharmacist|Pharm\.?\s*D\.?|Doctor of Pharmacy)\b", clause)
        if active_clinical_license:
            return reject("A current clinical professional license is required and is not established by the supplied background.")
        if (mandatory or listed_credential or qualification_section) and _has(credentials, clause):
            alternatives = _has(r"\b(?:or|and/or)\b.{0,65}\b(?:MBA|master|bachelor|equivalent experience|life sciences?|biology|neurobiology)", clause) or _has(r"\b(?:MBA|master|bachelor).{0,65}\bor\b", clause)
            if not alternatives:
                return reject("A mandatory clinical or doctoral credential is not established by the supplied background.")
            cautions.append("Degree alternatives need review; a clinical or doctoral credential is mentioned.")
        if _has(r"\b(?:U\.?S\.? citizen(?:ship)?|security clearance)\b", clause) and mandatory:
            cautions.append("Citizenship or security-clearance requirement needs review; no sponsorship need does not establish citizenship.")
        clause_minimum = _experience_minimum(clause)
        if clause_minimum is not None:
            required_years.append(clause_minimum)
    if required_years:
        minimum = max(required_years)
        if minimum > int(config.get("max_entry_years", 3)):
            return reject(f"Posting states at least {minimum} years of experience, above the configured entry-career search ceiling.")
        cautions.append(f"Posting mentions {minimum}+ years of experience; the user's matching experience has not been verified.")

    # Benefit boilerplate ('healthcare coverage') must not turn an unrelated
    # industry into a healthcare employer.
    sector_clauses = [c for c in clauses if not _has(r"\b(benefits?|insurance|dental|vision|401k|401\(k\))\b", c)]
    sector_text = " ".join([title, _text(job.get("industry")), _text(job.get("company"))] + sector_clauses)
    companies = config.get("healthcare_companies", [])
    if isinstance(companies, dict):
        companies = list(companies)
    known = _text(job.get("company")).casefold() in {_text(c).casefold() for c in companies}
    sector = known or _has(SECTOR, sector_text)
    if not sector:
        return reject("No biotech, pharmaceutical, medtech, or healthcare industry evidence.")
    reasons.append("Healthcare/life-science industry evidence" + (" from the configured employer watchlist." if known else " in the posting."))
    score = 25

    operations_override = _has(r"\b(?:role|position)\s+(?:is\s+)?in\s+operations\b.{0,70}\bnot\s+(?:in\s+)?(?:commercial|marketing)\b", description)
    if operations_override:
        score += 27
        reasons.append("Posting explicitly identifies an operations role rather than commercial/marketing (+27).")
    elif _has(COMMERCIAL, title):
        score += 45
        reasons.append("Primary commercial or strategy function (+45).")
    elif _has(SECONDARY, title):
        score += 37
        reasons.append("Marketing, market access, business development, or product function (+37).")
    elif _has(ADJACENT, title):
        score += 27
        reasons.append("Operations, finance, or adjacent business function (+27).")
    elif _has(RESEARCH, title):
        score += 18
        reasons.append("Science, clinical, regulatory, or research function (+18).")
    else:
        score += 8
        reasons.append("Other healthcare role; lower functional priority (+8).")

    early = program or _has(EARLY, title) or _has(r"\b(entry[ -]?level|early[ -]?career|new grad(?:uate)?|no (?:prior )?experience required)\b", description)
    if program:
        score += 15
        reasons.append("Internship or structured development/rotational program (+15).")
        cautions.append("Confirm current enrollment, graduation window, program dates, and availability.")
    elif early:
        score += 12
        reasons.append("Title or description suggests an early-career role (+12).")
    elif required_years and max(required_years) <= int(config.get("max_entry_years", 3)):
        score += 10
        reasons.append("Stated experience fits the configured early-career search range (+10).")
    elif _has(COMMERCIAL + "|" + SECONDARY + "|" + ADJACENT, title):
        cautions.append("Career level is unclear; included for business relevance, not confirmed entry-level eligibility.")
    else:
        return reject("No internship, rotational, early-career, or strong adjacent business-role signal.")

    if _has(r"\b(MBA|master(?:'s|s)? of business administration|business administration)\b", combined):
        score += 9
        reasons.append("Posting explicitly references an MBA/business-administration background (+9).")
    if _has(r"\b(neurobiology|neuroscience|neurotech(?:nology)?|biology|life sciences?|biotech(?:nology)?)\b", combined):
        score += 6
        reasons.append("Posting overlaps the supplied neurobiology/life-science background (+6).")
    strong_salary = _strong_salary(result["salary_text"], float(config.get("strong_salary_annual_usd", 100000)))
    if strong_salary:
        score += 3
        reasons.append("Disclosed pay meets the configured strong-pay threshold (+3); review compensation details.")
    if _has(r"\b(?:graduat(?:e|ed|ing|ion)|degree (?:completed|completion)|enrolled|enrollment)\b", description) and not program:
        cautions.append("Confirm degree-completion or graduation requirements and start-date availability.")
    if not description:
        cautions.append("Job description is unavailable; requirements have not been assessed.")
    cautions.append("Eligibility is not verified; check the employer's full required qualifications before applying.")
    result["score"] = min(score, 100)
    relevance_floor = int(config.get("min_score", 55))
    supported_adjacent = _has(COMMERCIAL + "|" + SECONDARY + "|" + ADJACENT + "|" + RESEARCH, title)
    if score < relevance_floor and not (strong_salary and supported_adjacent and score >= relevance_floor - 5):
        return reject("Relevance is below the configured review threshold; industry alone is not sufficient.")
    result["priority"] = "HIGH" if score >= 78 else ("MEDIUM" if score >= 60 else "PROMISING")
    # Avoid duplicate messages when multiple clauses repeat a condition.
    result["cautions"] = list(dict.fromkeys(cautions))
    result["eligibility"] = "Potential fit — verify requirements" if loc_status == "us" and early else "Check eligibility"
    return result
