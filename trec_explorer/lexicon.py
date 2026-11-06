"""Domain vocabulary shared by indexing and scenario analysis.

These are research aids, not legal classifications.  A concept fires only when
the scenario or the document actually uses one of its terms.
"""

from __future__ import annotations


CONCEPTS: dict[str, dict[str, object]] = {
    "agency": {
        "label": "Agency & fiduciary duties",
        "terms": [
            "agency", "fiduciary", "client", "principal", "represent",
            "representation", "intermediary", "dual capacity", "subagency",
            "loyalty", "obedience", "accounting",
        ],
    },
    "disclosure": {
        "label": "Disclosures & required notices",
        "terms": [
            "disclose", "disclosure", "notice", "iabs",
            "information about brokerage services", "consumer protection notice",
            "seller's disclosure", "material fact", "property condition",
            "flood", "lead-based paint", "agency relationship",
        ],
    },
    "advertising": {
        "label": "Advertising, signs & business names",
        "terms": [
            "advertise", "advertisement", "advertising", "sign", "team name",
            "assumed name", "dba", "website", "social media", "marketing",
            "solicit", "promote", "business name", "logo",
        ],
    },
    "commissions": {
        "label": "Commissions, fees & referrals",
        "terms": [
            "commission", "compensation", "fee", "referral", "rebate", "bonus",
            "paid", "payment", "split", "finder's fee", "thing of value",
        ],
    },
    "trust_funds": {
        "label": "Trust funds, escrow & deposits",
        "terms": [
            "trust money", "trust fund", "escrow", "earnest money", "deposit",
            "client funds", "commingle", "trust account", "trustee",
            "option fee", "security deposit",
        ],
    },
    "licensing": {
        "label": "Licensing, sponsorship & renewal",
        "terms": [
            "license", "licensure", "registration", "applicant", "sponsor",
            "broker license", "sales agent", "renewal", "inactive",
            "probationary", "qualifying experience", "fingerprint",
        ],
    },
    "broker_supervision": {
        "label": "Broker responsibility & supervision",
        "terms": [
            "supervise", "supervision", "sponsor", "broker responsibility",
            "policy", "procedures", "oversight", "delegate", "manage",
            "responsible for", "control",
        ],
    },
    "unlicensed": {
        "label": "Unlicensed activity & exemptions",
        "terms": [
            "unlicensed", "license required", "not required", "exempt",
            "attorney", "power of attorney", "owner", "referral",
            "for sale by owner", "fsbo",
        ],
    },
    "contracts": {
        "label": "Contracts & promulgated forms",
        "terms": [
            "contract", "addendum", "form", "promulgate", "buyer", "seller",
            "offer", "acceptance", "termination", "option", "lease",
            "amendment", "back-up", "contingency",
        ],
    },
    "inspections": {
        "label": "Inspections & standards of practice",
        "terms": [
            "inspect", "inspection", "inspector", "standards of practice",
            "deficiency", "report", "structural", "electrical", "hvac",
            "plumbing", "appliance", "roof", "foundation", "attic",
        ],
    },
    "inspector_conduct": {
        "label": "Inspector conduct, reports & insurance",
        "anchors": ["inspector", "inspection", "sop", "professional conduct"],
        "terms": [
            "professional conduct", "inspection report", "sop", "dual capacity",
            "undisclosed principal", "insurance", "sponsor", "pre-inspection",
            "contract", "advertise", "advertising",
        ],
    },
    "conflict": {
        "label": "Conflicts of interest & dual capacity",
        "anchors": [
            "dual capacity", "conflict of interest", "conflicting",
            "undisclosed principal", "own property", "repair",
            "broker and inspector", "inspector and broker",
        ],
        "terms": [
            "dual capacity", "conflict of interest", "conflicting",
            "undisclosed principal", "own property", "spouse", "parent",
            "child", "intermediary", "adverse", "competing", "repair",
            "broker", "inspector", "broker and inspector", "inspector and broker",
        ],
    },
    "timeshare": {
        "label": "Timeshare plans",
        "terms": [
            "timeshare", "timeshare plan", "purchaser", "developer", "escrow",
            "registration", "disclosure", "resale", "accommodation",
            "purchase contract", "timeshare interest",
        ],
    },
    "education": {
        "label": "Education & continuing education",
        "terms": [
            "education", "course", "continuing education", "ce", "instructor",
            "provider", "qualifying", "curriculum", "classroom", "distance education",
        ],
    },
    "discipline": {
        "label": "Complaints, discipline & penalties",
        "terms": [
            "complaint", "disciplinary", "suspend", "revoke", "penalty",
            "administrative penalty", "violation", "hearing", "sanction",
            "reprimand", "probation", "cease and desist",
        ],
    },
    "recovery": {
        "label": "Recovery funds",
        "terms": [
            "recovery fund", "recovery trust", "claim", "reimburse",
            "payment from", "trust account", "judgment",
        ],
    },
    "records": {
        "label": "Records & retention",
        "terms": [
            "record", "retain", "retention", "document", "file", "maintain",
            "four years", "5 years", "bookkeeping", "accounting",
        ],
    },
    "consumer": {
        "label": "Consumer protection & fair dealing",
        "terms": [
            "consumer", "client", "customer", "fair", "misrepresent", "fraud",
            "deceptive", "discriminate", "honest", "good faith",
            "untrustworthiness", "dishonesty",
        ],
    },
    "property_management": {
        "label": "Rental & property management",
        "terms": [
            "rental", "lease", "landlord", "tenant", "property management",
            "residential rental locator", "rent", "management agreement",
        ],
    },
    "erw": {
        "label": "Easement / right-of-way practice",
        "terms": [
            "easement", "right-of-way", "right of way", "erw", "utility",
            "pipeline", "telecommunication", "railroad", "condemnation",
        ],
    },
    "appraisal": {
        "label": "Appraisals & price opinions",
        "terms": [
            "appraisal", "appraiser", "broker price opinion", "bpo",
            "comparative market analysis", "cma", "opinion of value",
            "price estimate", "market analysis",
        ],
    },
    "procedural": {
        "label": "Practice, procedure & deadlines",
        "terms": [
            "hearing", "notice", "motion", "soah", "deadline", "days",
            "appeal", "rehearing", "subpoena", "default", "jurisdiction",
            "final order", "proposal for decision",
        ],
    },
}


STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "also", "am",
    "an", "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "could", "did",
    "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those", "through",
    "to", "too", "under", "until", "up", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with",
    "would", "you", "your", "yours", "yourself", "yourselves", "scenario",
    "client", "wants", "want", "need", "needs", "asking", "ask", "situation",
    "question", "possible", "whether", "may", "must", "shall", "asked",
    "says", "said", "told", "another", "one", "two", "three", "just",
}


# Multi-word phrases worth preserving as exact FTS phrases.
PHRASES = [
    "trust money", "trust fund", "earnest money", "dual capacity",
    "undisclosed principal", "assumed name", "team name", "business name",
    "information about brokerage services", "consumer protection notice",
    "standards of practice", "right of way", "right-of-way", "broker price opinion",
    "comparative market analysis", "material fact", "seller's disclosure",
    "recovery fund", "broker responsibility", "continuing education",
    "administrative penalty", "residential rental locator", "power of attorney",
    "property management", "inspection report", "purchase contract",
]


# Small, transparent issue lenses used to surface the short, decisive section
# when a long standards-of-practice section would otherwise dominate keyword
# ranking.  These are navigation hints, not conclusions of law.
ISSUE_LENSES: list[dict[str, object]] = [
    {
        "id": "advertising_names",
        "label": "Advertising, team names & unlicensed referrals",
        "requires": ["advertising", "unlicensed"],
        "title_terms": [
            "advertisements", "alternate, team and assumed", "splitting fee",
            "referrals from unlicensed", "license required", "advertising",
        ],
        "why": "The facts combine advertising or a business name with a referral, fee, or licensing question.",
    },
    {
        "id": "inspector_conflict",
        "label": "Inspector conflicts, repairs & side work",
        "requires": ["inspections", "conflict"],
        "title_terms": [
            "conflicting capacities", "repairs and maintenance",
            "professional conduct", "inspection report",
            "standards of practice: general",
        ],
        "why": "The facts combine an inspection with a broker relationship or repair work.",
    },
    {
        "id": "trust_money",
        "label": "Trust money, escrow & deposits",
        "requires": ["trust_funds"],
        "title_terms": [
            "maintaining trust money", "trust account", "escrow",
            "earnest money", "security deposit",
        ],
        "why": "The facts involve money held for someone else.",
    },
    {
        "id": "disclosure_agency",
        "label": "Disclosure, agency & representation",
        "requires": ["disclosure"],
        "title_terms": [
            "information about brokerage services", "consumer information",
            "disclosures", "agency", "representation", "intermediary",
        ],
        "why": "The facts raise what must be disclosed or whose interests are represented.",
    },
    {
        "id": "timeshare",
        "label": "Timeshare registration & sales",
        "requires": ["timeshare"],
        "title_terms": [
            "registration", "disclosure", "purchase contract", "escrow",
            "right to cancel", "advertisements",
        ],
        "why": "The facts involve a timeshare plan or timeshare interest.",
    },
    {
        "id": "supervision",
        "label": "Broker supervision & sponsorship",
        "requires": ["broker_supervision", "licensing"],
        "title_terms": [
            "broker responsibility", "sponsorship", "supervision",
            "license required", "sponsor",
        ],
        "why": "The facts involve who supervises or sponsors the license holder.",
    },
]


def matched_issue_lenses(concepts: list[tuple[str, str, int]]) -> list[dict[str, object]]:
    keys = {key for key, _, _ in concepts}
    return [
        lens for lens in ISSUE_LENSES
        if all(required in keys for required in lens["requires"])  # type: ignore[operator]
    ]

