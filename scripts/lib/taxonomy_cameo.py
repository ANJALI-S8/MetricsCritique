"""
Global Geopolitical Event & Topic Coding Framework v1.0 -- machine-readable taxonomy.

CAMEO/GDELT-compatible, actor-agnostic. Two orthogonal axes:

  * SECTION A (codes 01-10, + 41 sub-codes) -- WHAT the document is ABOUT (subject).
    Replaces the original CAMEO verb-based codes 01-12 with thematic domains.
  * SECTION B (codes 13-20) -- WHAT KIND of action occurred (escalation ladder),
    retained unchanged from the CAMEO/GDELT codebook.
  * Codes 11-12 are intentionally reserved/unused, preserving CAMEO's numbering so
    that 13 remains the start of the conflict-escalation ladder.

A document gets >=1 Section A code; it gets a Section B code only if it also
describes a conflictual or coercive action. Multi-label on both axes.

------------------------------------------------------------------------------
WHY THIS FILE EXISTS (and why the taxonomy is data, not code)
------------------------------------------------------------------------------
The taxonomy it replaces was duplicated in three places that had to be
hand-synchronised: CATEGORY_DESCRIPTIONS + NLI_HYPOTHESES in
scripts/12_risk_theme_classification.py, and RISK_META/RISK_ORDER/THEME_META/
THEME_ORDER in UI/assets/dash.js. The dash.js arrays *were* the effective UI
schema -- any category missing from them was silently dropped from every panel,
so a taxonomy change failed invisibly rather than loudly. Here the taxonomy is
authored once and emitted to data/taxonomy_cameo.json, which both the classifier
and the dashboard read.

------------------------------------------------------------------------------
DESIGN DECISIONS, and the measurements behind them
------------------------------------------------------------------------------
1. KEYS ARE FULLY-QUALIFIED STRINGS, never bare numbers. The predecessor's
   NLI_HYPOTHESES was a FLAT dict looked up by bare category name, so two axes
   sharing a key would silently collide. `a05_2_maritime_disputes` /
   `b18_assault` cannot collide, and the numeric `code` is a separate field.

2. NLI HYPOTHESES ARE SHORT AND SINGLE-CLAUSE. Calibration on this corpus
   measured that reusing the long disjunctive bi-encoder descriptions as NLI
   hypotheses performs "markedly worse". Descriptions are for the bi-encoder
   recall net (cosine against bge-m3); hypotheses are for NLI adjudication.
   They are deliberately NOT the same text.

3. DESCRIPTIONS ARE EXCLUSION-AWARE. Adjacent classes are the failure mode: the
   prior audit found 192 posts carrying >=6 of 9 categories. Where two classes
   are easily confused, the description says what the class is NOT.

4. `alertable` REFLECTS MEASURED SPARSITY, not preference. Daily anomaly
   detection needs baseline_mean >= 0.5/day to clear the support gate. A lexical
   probe over the 20,347 usable-text posts gave these English-cue floors:
       18 ASSAULT 14.5% | 14 PROTEST 11.3% | 15 FORCE POSTURE 5.4%
       13 THREATEN 4.6% | 19 FIGHT 4.3%    | 17 COERCE 3.6%
       16 REDUCE RELATIONS 1.2%            | 20 MASS VIOLENCE 0.3%
   Code 20 (~52 posts, ~1.1/day) can never clear the gate, and 16 is marginal.
   Both are still CODED -- they belong in the record -- but flagged
   alertable=False so they do not generate permanently-empty alert panels.
   These are recall FLOORS from English keywords only; the true prevalence in
   Russian/Chinese/Bengali/Hindi/Ukrainian text is higher and unmeasured.

5. `crosswalk_from` RECORDS THE OLD CATEGORY THIS CODE ABSORBS, so agreement
   between old and new labels can be MEASURED on the same posts rather than
   assumed. The framework specifies two merges (01 absorbs political+
   international relations; 05 absorbs maritime_security+maritime_dimensions).
   The old 9 "risk" categories do not map to Section A at all -- they were a
   conflation of subject and action, which is precisely what the two-axis split
   resolves; their crosswalk targets are recorded on both axes.
"""
from __future__ import annotations

import json
from pathlib import Path

EDA_ROOT = Path(__file__).resolve().parents[2]
JSON_OUT = EDA_ROOT / "data" / "taxonomy_cameo.json"

FRAMEWORK_VERSION = "1.0"
RESERVED_CODES = ["11", "12"]  # kept unused so 13 stays the start of the escalation ladder

# ---------------------------------------------------------------- SECTION A --
# key -> (code, display, description_for_biencoder, nli_hypothesis, crosswalk_from)
# Parents carry level=1; sub-codes level=2 with parent_code set.

SECTION_A: dict = {
    "a01_political_diplomatic_relations": {
        "code": "01", "display": "Political & Diplomatic Relations",
        "description": (
            "Government-to-government interaction: official visits, summits, bilateral or "
            "multilateral political dialogue, diplomatic statements, and political treaties "
            "between states. Diplomacy and political relations as such -- not trade terms, "
            "not military operations, not border incidents."),
        "hypothesis": "This text describes diplomatic or political relations between governments.",
        "crosswalk_from": ["political_relations", "international_relations"],
        "subs": {
            "a01_1_bilateral_political": {
                "code": "01.1", "display": "Bilateral Political Relations",
                "description": "Official state visits, bilateral summits, and political talks between two named governments.",
                "hypothesis": "This text describes a bilateral meeting or state visit between two governments."},
            "a01_2_multilateral": {
                "code": "01.2", "display": "Multilateral & International Relations",
                "description": "Engagement in multilateral bodies and regional groupings: UN votes, regional-bloc summits, joint communiques of groupings.",
                "hypothesis": "This text describes multilateral diplomacy in an international organisation or regional bloc."},
            "a01_3_diplomatic_statements": {
                "code": "01.3", "display": "Diplomatic Statements & Positioning",
                "description": "Official statements, foreign-ministry briefings, ambassadorial remarks, and diplomatic signalling on political issues.",
                "hypothesis": "This text reports an official government or foreign-ministry statement."},
            "a01_4_treaties_agreements": {
                "code": "01.4", "display": "Treaties & Agreements",
                "description": "Signing, ratification, suspension, or withdrawal from political and strategic agreements, treaties, and memoranda of understanding.",
                "hypothesis": "This text describes a treaty, pact, or memorandum being signed, ratified, or suspended.",
                "crosswalk_from": ["policy_shifts"]},
        },
    },
    "a02_economic_relations": {
        "code": "02", "display": "Economic Relations",
        "description": (
            "Trade, investment, and financial-policy interaction between states: tariffs, trade "
            "disputes, FDI, currency and debt arrangements, and economic sanctions. Excludes "
            "activity tied to a named connectivity initiative (code 03) and energy trade (code 04)."),
        "hypothesis": "This text describes trade, investment, or financial relations between countries.",
        "crosswalk_from": ["economic_relations"],
        "subs": {
            "a02_1_trade": {
                "code": "02.1", "display": "Trade Relations",
                "description": "Imports and exports, tariffs, trade deficits, trade disputes, and free-trade agreements between states.",
                "hypothesis": "This text describes trade flows, tariffs, or a trade agreement between countries."},
            "a02_2_investment_fdi": {
                "code": "02.2", "display": "Investment & FDI",
                "description": "Inbound or outbound foreign direct investment, cross-border acquisitions, investment screening, and special economic zones.",
                "hypothesis": "This text describes foreign investment or a cross-border acquisition."},
            "a02_3_financial_monetary": {
                "code": "02.3", "display": "Financial & Monetary Cooperation",
                "description": "Currency swap lines, sovereign loans, debt restructuring, and central-bank cooperation between states.",
                "hypothesis": "This text describes a loan, currency swap, or debt arrangement between countries."},
            "a02_4_sanctions_restrictions": {
                "code": "02.4", "display": "Economic Sanctions & Restrictions",
                "description": "Economic coercion between states: sanctions, embargoes, import bans, and export restrictions. The economic measure itself, as subject matter.",
                "hypothesis": "This text describes economic sanctions or trade restrictions imposed by one country on another.",
                "crosswalk_from": ["sanctions"]},
        },
    },
    "a03_infrastructure_connectivity": {
        "code": "03", "display": "Strategic Infrastructure & Connectivity Initiatives",
        "description": (
            "Cross-border infrastructure, connectivity, and development-financing initiatives "
            "sponsored by any state, bloc, or multilateral body -- ports, railways, corridors, "
            "and the debt and diplomacy attached to them. Actor-neutral: applies to any "
            "sponsoring state, not one named programme."),
        "hypothesis": "This text describes a cross-border infrastructure or connectivity initiative.",
        "crosswalk_from": ["bri"],
        "subs": {
            "a03_1_infrastructure_projects": {
                "code": "03.1", "display": "Infrastructure Projects",
                "description": "Ports, railways, roads, and special economic zones built under a connectivity or development initiative.",
                "hypothesis": "This text describes construction of a port, railway, road, or economic zone."},
            "a03_2_project_financing_debt": {
                "code": "03.2", "display": "Project Financing & Debt",
                "description": "Loans, debt-sustainability concerns, defaults, and restructuring tied to initiative-linked infrastructure.",
                "hypothesis": "This text describes project debt, a loan, or debt sustainability for an infrastructure project."},
            "a03_3_named_corridors": {
                "code": "03.3", "display": "Named Corridors & Programs",
                "description": "A specifically named economic corridor, connectivity programme, or initiative brand referenced in reporting.",
                "hypothesis": "This text names a specific economic corridor or connectivity programme."},
            "a03_4_initiative_status": {
                "code": "03.4", "display": "Initiative Diplomacy & Project Status",
                "description": "Initiative summits, new project agreements, and project delays, cancellations, or withdrawals.",
                "hypothesis": "This text describes an infrastructure project being delayed, cancelled, or newly agreed.",
                "crosswalk_from": ["project_delays"]},
        },
    },
    "a04_energy_security": {
        "code": "04", "display": "Energy Security",
        "description": (
            "Energy interests, energy infrastructure, resource access, and energy diplomacy, "
            "including effects on partner and rival states. Energy as the subject -- pipelines, "
            "LNG, nuclear cooperation, chokepoint dependency."),
        "hypothesis": "This text describes energy supply, energy infrastructure, or energy security.",
        "crosswalk_from": ["energy_security"],
        "subs": {
            "a04_1_oil_gas_access": {
                "code": "04.1", "display": "Oil & Gas Access",
                "description": "Pipelines, oil and gas supply deals, LNG contracts, and import-route dependency.",
                "hypothesis": "This text describes oil or gas supply, a pipeline, or an LNG deal."},
            "a04_2_nuclear_renewable": {
                "code": "04.2", "display": "Nuclear & Renewable Energy Cooperation",
                "description": "Nuclear power plant construction or agreements, and export of renewable-energy technology.",
                "hypothesis": "This text describes nuclear power or renewable energy cooperation between countries."},
            "a04_3_energy_infra_security": {
                "code": "04.3", "display": "Energy Infrastructure Security",
                "description": "Protection of, or threat to, pipelines, energy ports, refineries, and energy corridors.",
                "hypothesis": "This text describes a threat to, or the protection of, energy infrastructure such as a pipeline.",
                "crosswalk_from": ["pipeline_vulnerabilities"]},
            "a04_4_energy_competition": {
                "code": "04.4", "display": "Energy-related Strategic Competition",
                "description": "Strategic competition over energy routes, resources, and maritime or overland chokepoints.",
                "hypothesis": "This text describes strategic competition over energy routes or resources."},
        },
    },
    "a05_maritime": {
        "code": "05", "display": "Maritime Security & Dimensions",
        "description": (
            "Maritime activity in both its military/strategic and its broader maritime-affairs "
            "sense: naval operations and deployments, sea disputes, shipping lanes and "
            "chokepoints, port access, and fishing/EEZ governance."),
        "hypothesis": "This text describes naval activity, a maritime dispute, or shipping and sea lanes.",
        "crosswalk_from": ["maritime_security", "maritime_dimensions"],
        "subs": {
            "a05_1_naval_presence": {
                "code": "05.1", "display": "Naval Presence & Deployments",
                "description": "Naval deployments, port calls, patrols, and carrier or fleet movements by any state's navy.",
                "hypothesis": "This text describes a naval deployment, patrol, or warship movement."},
            "a05_2_maritime_disputes": {
                "code": "05.2", "display": "Territorial & Maritime Disputes",
                "description": "Contested waters, disputed islands and reefs, and exclusive economic zone incursions between states.",
                "hypothesis": "This text describes a dispute over contested waters, islands, or an exclusive economic zone."},
            "a05_3_sloc": {
                "code": "05.3", "display": "Sea Lines of Communication (SLOC)",
                "description": "Strategic shipping routes, straits and chokepoints, and the security or disruption of commercial shipping.",
                "hypothesis": "This text describes a strategic shipping route, strait, or chokepoint.",
                "crosswalk_from": ["shipping_disruptions"]},
            "a05_4_port_access_dual_use": {
                "code": "05.4", "display": "Port Access & Dual-use Infrastructure",
                "description": "Overseas port leases, logistics facilities, and infrastructure with potential dual military-civilian use.",
                "hypothesis": "This text describes an overseas port lease or a dual-use logistics facility."},
            "a05_5_maritime_law": {
                "code": "05.5", "display": "Maritime Law & Governance",
                "description": "UNCLOS rulings and arbitration, fishing rights disputes, and blue-economy governance.",
                "hypothesis": "This text describes maritime law, an UNCLOS ruling, or fishing rights."},
        },
    },
    "a06_security_military_relations": {
        "code": "06", "display": "Security & Military Relations",
        "description": (
            "Defence relations BETWEEN states: joint exercises, arms transfers, force "
            "modernisation, and alliance formation. Cooperative and capability-building "
            "military activity -- not the conduct of hostilities, which is coded on the "
            "Section B escalation axis."),
        "hypothesis": "This text describes defence cooperation, arms transfers, or military capability development.",
        "crosswalk_from": ["security_military_relations"],
        "subs": {
            "a06_1_mil_to_mil": {
                "code": "06.1", "display": "Military-to-Military Engagement",
                "description": "Joint military exercises, defence dialogues, staff talks, and officer exchanges between states.",
                "hypothesis": "This text describes a joint military exercise or defence dialogue between countries."},
            "a06_2_arms_sales": {
                "code": "06.2", "display": "Arms Sales & Defense Trade",
                "description": "Weapons transfers, defence procurement contracts, and military equipment sales between states.",
                "hypothesis": "This text describes an arms sale or defence equipment deal."},
            "a06_3_modernization": {
                "code": "06.3", "display": "Military Modernization & Capability Development",
                "description": "A state's force modernisation, new weapons systems, and defence capability build-up.",
                "hypothesis": "This text describes military modernisation or a new weapons capability."},
            "a06_4_alliances": {
                "code": "06.4", "display": "Alliance & Security Partnerships",
                "description": "Formal or informal security alignments, defence pacts, and security partnerships between states.",
                "hypothesis": "This text describes a security alliance, defence pact, or military partnership."},
        },
    },
    "a07_border_issues": {
        "code": "07", "display": "Border Issues",
        "description": (
            "Land-border developments between neighbouring states: disputed sectors, border "
            "infrastructure, confidence-building talks, and border incidents. Land borders "
            "specifically -- maritime boundaries are code 05."),
        "hypothesis": "This text describes a land border between countries.",
        "crosswalk_from": ["border_issues"],
        "subs": {
            "a07_1_border_disputes": {
                "code": "07.1", "display": "Territorial / Border Disputes",
                "description": "Disputed border sectors, competing territorial claims, and standoffs along a contested land border.",
                "hypothesis": "This text describes a disputed land border or territorial claim."},
            "a07_2_border_infrastructure": {
                "code": "07.2", "display": "Border Infrastructure",
                "description": "Road, rail, and settlement construction near a border, including dual-use border villages.",
                "hypothesis": "This text describes construction of roads, rail, or settlements near a border."},
            "a07_3_border_talks_cbm": {
                "code": "07.3", "display": "Border Talks & CBMs",
                "description": "Special-representative talks, hotline mechanisms, disengagement and de-escalation agreements on a border.",
                "hypothesis": "This text describes border talks, a disengagement agreement, or a confidence-building measure."},
            "a07_4_border_incidents": {
                "code": "07.4", "display": "Border Incidents",
                "description": "Skirmishes, incursions, patrol face-offs, and firing incidents at a land border.",
                "hypothesis": "This text describes a skirmish, incursion, or face-off at a border.",
                "crosswalk_from": ["border_conflicts"]},
        },
    },
    "a08_internal_conflict_external_role": {
        "code": "08", "display": "Internal Conflict & External Actor Role",
        "description": (
            "Internal conflict, insurgency, or political instability within a state, and the "
            "role played by any external state or non-state actor -- mediation, support, or "
            "influence -- plus a state's own external messaging about its internal stability. "
            "Actor-neutral: no specific country is privileged."),
        "hypothesis": "This text describes internal conflict or instability within a country.",
        "crosswalk_from": ["internal_conflict_china_role", "insurgencies"],
        "subs": {
            "a08_1_mediation": {
                "code": "08.1", "display": "Mediation & Conflict Resolution",
                "description": "An outside state or organisation acting as mediator, broker, or facilitator in another country's conflict.",
                "hypothesis": "This text describes an outside country mediating or brokering talks in another country's conflict."},
            "a08_2_external_support": {
                "code": "08.2", "display": "External Support to State / Non-state Actors",
                "description": "Reported or alleged external backing of a government or of armed factions in a conflict zone.",
                "hypothesis": "This text describes an outside country backing a government or armed group in a conflict."},
            "a08_3_domestic_stability_narratives": {
                "code": "08.3", "display": "Domestic Stability Narratives",
                "description": "A government's external messaging about its own internal unrest, and international reaction to it.",
                "hypothesis": "This text describes political unrest or instability inside one country.",
                "crosswalk_from": ["domestic_instability"]},
            "a08_4_spillover_refugees": {
                "code": "08.4", "display": "Refugee & Cross-border Spillover",
                "description": "Conflict spilling across a border, displacement, and refugee flows affecting neighbouring states.",
                "hypothesis": "This text describes refugees or conflict spilling across a border into a neighbouring country."},
        },
    },
    "a09_technology_digital": {
        "code": "09", "display": "Technology & Digital Influence",
        "description": (
            "Technology exports, digital-infrastructure projects, cyber activity, and influence "
            "exerted through technological means: 5G and telecom, undersea cables, surveillance "
            "technology, cyber operations, and export controls on strategic technology."),
        "hypothesis": "This text describes technology, digital infrastructure, or cyber activity between countries.",
        "crosswalk_from": ["tech_digital_influence"],
        "subs": {
            "a09_1_digital_infrastructure": {
                "code": "09.1", "display": "Digital Infrastructure & Connectivity",
                "description": "5G rollout, telecom network contracts, undersea cables, and data-centre projects across borders.",
                "hypothesis": "This text describes telecom networks, 5G, undersea cables, or data centres."},
            "a09_2_surveillance_tech": {
                "code": "09.2", "display": "Surveillance & Security Tech Exports",
                "description": "Export or transfer of surveillance systems, facial recognition, and policing technology.",
                "hypothesis": "This text describes export or transfer of surveillance or facial-recognition technology."},
            "a09_3_cyber_activity": {
                "code": "09.3", "display": "Cyber Activity",
                "description": "Cyberattacks, cyber espionage, attribution of intrusions, and cybersecurity cooperation or dispute.",
                "hypothesis": "This text describes a cyberattack, cyber espionage, or cybersecurity."},
            "a09_4_tech_standards_controls": {
                "code": "09.4", "display": "Tech Standards & Export Controls",
                "description": "Semiconductor and technology export controls, technology-transfer restrictions, and standard-setting disputes.",
                "hypothesis": "This text describes technology export controls or semiconductor restrictions."},
        },
    },
    "a10_humanitarian": {
        "code": "10", "display": "Humanitarian Dimension",
        "description": (
            "Humanitarian assistance, disaster relief, public-health diplomacy, and related "
            "soft-power activity by any state or organisation."),
        "hypothesis": "This text describes humanitarian aid, disaster relief, or medical assistance.",
        "crosswalk_from": ["humanitarian_dimension"],
        # Measured, not assumed. On the 872-pair hand-judged stratified sample
        # (data/calibration_cameo.csv) the sampling-weighted population positive
        # rate for code 10 is 0.16% -- about 34 posts across the whole 46-day
        # window, ~0.7/day, and that estimate rests on only 4 judged positives
        # even after a targeted second sampling round that oversampled the top
        # score bands specifically to find more. That is the same structural
        # situation as codes 16 and 20: the trend-alert support gate needs
        # baseline_mean >= 0.5/day, which this can only ever scrape, so an
        # alert panel here would be permanently empty or driven by one or two
        # posts. It is still CODED -- a humanitarian event belongs in the
        # record -- it is just not trend-alerted.
        "alertable": False,
        "alertable_reason": (
            "Measured population rate 0.16% (~34 posts / 46 days, ~0.7/day) from the "
            "872-pair judged sample, on only 4 judged positives after targeted "
            "oversampling. Too sparse to clear the baseline_mean >= 0.5/day support "
            "gate reliably; coded for the record but not trend-alerted."),
        "subs": {
            "a10_1_disaster_relief": {
                "code": "10.1", "display": "Disaster Relief & Emergency Aid",
                "description": "Earthquake, flood, cyclone, or other emergency relief assistance sent across borders.",
                "hypothesis": "This text describes disaster relief or emergency aid after a natural disaster."},
            "a10_2_health_diplomacy": {
                "code": "10.2", "display": "Health & Medical Diplomacy",
                "description": "Vaccine donations, medical team deployments, and hospital construction as international assistance.",
                "hypothesis": "This text describes medical assistance, vaccine donation, or health diplomacy."},
            "a10_3_conflict_zone_aid": {
                "code": "10.3", "display": "Humanitarian Assistance in Conflict Zones",
                "description": "Aid convoys and relief delivery into an active conflict or siege setting.",
                "hypothesis": "This text describes humanitarian aid being delivered into a conflict zone."},
            "a10_4_refugee_migration_aid": {
                "code": "10.4", "display": "Refugee & Migration-related Assistance",
                "description": "Assistance programmes directed at refugee populations or migration situations.",
                "hypothesis": "This text describes assistance for refugees or migrants."},
        },
    },
}

# ---------------------------------------------------------------- SECTION B --
# Retained unchanged from CAMEO/GDELT. These classify the NATURE and INTENSITY of
# an action, independent of subject and of who the actors are. `alertable` is set
# from the measured sparsity floors documented in the module docstring.
#
# `hypothesis_template` carries an optional {actor1} slot, following the templated
# pattern already proven for entity subtyping. It is used only where a dyad was
# recovered; `hypothesis` is the actor-free fallback.

SECTION_B: dict = {
    "b13_threaten": {
        "code": "13", "display": "Threaten", "alertable": True,
        "description": (
            "Threats to attack, coerce, boycott, sanction, or halt negotiations or aid unless "
            "demands are met -- including threats of force, non-force threats, and unspecified "
            "threats. The threat, not its execution."),
        "hypothesis": "This text describes one party threatening another.",
        "hypothesis_template": "{actor1} threatened another party.",
        "crosswalk_from": ["international_pressure"]},
    "b14_protest": {
        "code": "14", "display": "Protest", "alertable": True,
        "description": (
            "Public demonstrations of dissatisfaction: demonstrations, rallies, hunger strikes, "
            "strikes and boycotts, obstruction of passage, and protests that turn violent."),
        "hypothesis": "This text describes a protest, demonstration, or strike.",
        "hypothesis_template": "People protested against {actor1}."},
    "b15_force_posture": {
        "code": "15", "display": "Exhibit Force Posture", "alertable": True,
        "description": (
            "Military posturing short of engagement: alerts and readiness changes, mobilisation "
            "or reinforcement of police or military power, and military, naval, or air "
            "deployments and manoeuvres. Signalling capability without using it."),
        "hypothesis": "This text describes a military mobilisation, alert, or show of force.",
        "hypothesis_template": "{actor1} mobilised or deployed military forces."},
    "b16_reduce_relations": {
        "code": "16", "display": "Reduce Relations", "alertable": False,
        "alertable_reason": (
            "Measured English-cue floor 1.2% of usable-text posts (~245 posts, ~5/day). "
            "Marginal against the baseline_mean >= 0.5/day support gate; coded for the record "
            "but excluded from daily alerting to avoid a mostly-empty panel."),
        "description": (
            "Actions reducing the normal level of international interaction: breaking or "
            "downgrading diplomatic relations, halting aid, imposing embargoes or boycotts, "
            "ending negotiations, and expelling or withdrawing personnel."),
        "hypothesis": "This text describes one country cutting ties, expelling diplomats, or halting cooperation.",
        "hypothesis_template": "{actor1} reduced or severed relations with another party."},
    "b17_coerce": {
        "code": "17", "display": "Coerce", "alertable": True,
        "description": (
            "Actions to pressure or force a change in behaviour: seizing or damaging property, "
            "administrative sanctions, arrests, detentions and expulsions of individuals, and "
            "violation of territory."),
        "hypothesis": "This text describes arrests, detentions, seizures, or a blockade.",
        "hypothesis_template": "{actor1} detained, seized, or coerced another party."},
    "b18_assault": {
        "code": "18", "display": "Assault", "alertable": True,
        "description": (
            "Physical acts of violence, sabotage, or hijacking, and lethal or non-lethal attacks "
            "that do not amount to sustained fighting. Discrete attacks rather than continuous "
            "combat."),
        "hypothesis": "This text describes an attack, bombing, or act of sabotage.",
        "hypothesis_template": "{actor1} carried out an attack."},
    "b19_fight": {
        "code": "19", "display": "Fight", "alertable": True,
        "description": (
            "Open, extensive, continuous militarised conflict: conventional military force, "
            "blockades, occupation of territory, engagements with small or heavy weapons, "
            "violent repression, and cyberwarfare. Sustained combat rather than a single attack."),
        "hypothesis": "This text describes sustained armed fighting, shelling, or military occupation.",
        "hypothesis_template": "{actor1} engaged in sustained armed combat."},
    "b20_mass_violence": {
        "code": "20", "display": "Engage in Unconventional Mass Violence", "alertable": False,
        "alertable_reason": (
            "Measured English-cue floor 0.3% of usable-text posts (~52 posts, ~1.1/day). "
            "Structurally cannot clear the baseline_mean >= 0.5/day support gate over this "
            "window, so it would produce a permanently empty alert panel. Still coded -- an "
            "atrocity claim belongs in the record even when it cannot be trend-alerted."),
        "description": (
            "Use of weapons of mass destruction, and mass expulsion, mass killing, ethnic "
            "cleansing, or genocide."),
        "hypothesis": "This text describes mass killing, ethnic cleansing, genocide, or use of weapons of mass destruction.",
        "hypothesis_template": "{actor1} committed mass violence against a population."},
}


def build() -> dict:
    """Flatten the authored taxonomy into the emitted contract.

    `codes` is a flat list -- one entry per scoreable class -- because the
    classifier needs positional alignment between its similarity-matrix columns
    and (axis, key). Hierarchy is carried by `parent_key`/`level`, not by nesting,
    so a consumer can iterate without recursion.
    """
    codes: list[dict] = []
    for pkey, p in SECTION_A.items():
        codes.append({
            "key": pkey, "axis": "theme", "level": 1, "code": p["code"],
            "parent_key": None, "parent_code": None, "display": p["display"],
            "description": p["description"], "hypothesis": p["hypothesis"],
            # Honour the authored value. This used to be a hardcoded True, which
            # meant an `alertable: False` written on a Section A code was read,
            # validated, and then silently dropped on the way out -- the exact
            # class of invisible failure this taxonomy exists to prevent.
            "alertable": p.get("alertable", True),
            "alertable_reason": p.get("alertable_reason"),
            "crosswalk_from": p.get("crosswalk_from", []),
        })
        for skey, s in p["subs"].items():
            codes.append({
                "key": skey, "axis": "theme", "level": 2, "code": s["code"],
                "parent_key": pkey, "parent_code": p["code"], "display": s["display"],
                "description": s["description"], "hypothesis": s["hypothesis"],
                # Sub-codes inherit the parent's alertability. A child cannot be
                # alertable when its parent is too sparse to alert on -- it is
                # by construction rarer still. A further volume floor is applied
                # downstream in the metrics stage.
                "alertable": s.get("alertable", p.get("alertable", True)),
                "alertable_reason": s.get(
                    "alertable_reason",
                    None if p.get("alertable", True)
                    else f"inherited from parent {p['code']}: {p.get('alertable_reason')}"),
                "crosswalk_from": s.get("crosswalk_from", []),
            })
    for akey, a in SECTION_B.items():
        codes.append({
            "key": akey, "axis": "action", "level": 1, "code": a["code"],
            "parent_key": None, "parent_code": None, "display": a["display"],
            "description": a["description"], "hypothesis": a["hypothesis"],
            "hypothesis_template": a.get("hypothesis_template"),
            "alertable": a["alertable"],
            "alertable_reason": a.get("alertable_reason"),
            "crosswalk_from": a.get("crosswalk_from", []),
        })
    return {
        "framework": "Global Geopolitical Event & Topic Coding Framework",
        "framework_version": FRAMEWORK_VERSION,
        "reserved_codes": RESERVED_CODES,
        "reserved_note": (
            "Codes 11-12 are intentionally unused so that code 13 remains the start of the "
            "CAMEO conflict-escalation ladder."),
        "axes": {
            "theme": "Section A -- WHAT the document is about (subject matter). At least one required.",
            "action": "Section B -- WHAT KIND of action occurred (escalation ladder). Conditional.",
        },
        "codes": codes,
    }


def validate(tax: dict) -> list[str]:
    """Structural invariants the rest of the pipeline relies on."""
    errs: list[str] = []
    codes = tax["codes"]
    keys = [c["key"] for c in codes]
    if len(keys) != len(set(keys)):
        dup = sorted({k for k in keys if keys.count(k) > 1})
        errs.append(f"duplicate keys (would silently collide in the flat NLI lookup): {dup}")
    # Numeric codes must be unique WITHIN an axis, and no code may land on 11/12.
    for axis in ("theme", "action"):
        nums = [c["code"] for c in codes if c["axis"] == axis]
        if len(nums) != len(set(nums)):
            errs.append(f"duplicate numeric codes within axis {axis}")
    for c in codes:
        if c["code"].split(".")[0] in RESERVED_CODES:
            errs.append(f"{c['key']} uses reserved code {c['code']}")
        if c["level"] == 2 and not c["parent_key"]:
            errs.append(f"{c['key']} is level 2 but has no parent_key")
        if c["level"] == 2 and not c["code"].startswith(c["parent_code"] + "."):
            errs.append(f"{c['key']} code {c['code']} inconsistent with parent {c['parent_code']}")
        for f in ("display", "description", "hypothesis"):
            if not str(c.get(f) or "").strip():
                errs.append(f"{c['key']} missing {f}")
        # Hypotheses must stay short/single-clause -- measured to matter on this corpus.
        if len(c["hypothesis"].split()) > 22:
            errs.append(f"{c['key']} hypothesis too long ({len(c['hypothesis'].split())} words)")
    # Every retired category must be absorbed somewhere, or we have lost coverage.
    absorbed = {old for c in codes for old in c["crosswalk_from"]}
    OLD = {"political_relations", "international_relations", "economic_relations", "bri",
           "energy_security", "maritime_security", "maritime_dimensions",
           "security_military_relations", "border_issues", "internal_conflict_china_role",
           "tech_digital_influence", "humanitarian_dimension",
           "domestic_instability", "policy_shifts", "project_delays", "sanctions",
           "border_conflicts", "insurgencies", "shipping_disruptions",
           "pipeline_vulnerabilities", "international_pressure"}
    missing = sorted(OLD - absorbed)
    if missing:
        errs.append(f"old categories with no crosswalk target (coverage loss): {missing}")
    return errs


def main() -> int:
    tax = build()
    errs = validate(tax)
    if errs:
        print("TAXONOMY VALIDATION FAILED:")
        for e in errs:
            print("  -", e)
        return 1
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(tax, ensure_ascii=False, indent=2), encoding="utf-8")
    codes = tax["codes"]
    n_theme_p = sum(1 for c in codes if c["axis"] == "theme" and c["level"] == 1)
    n_theme_s = sum(1 for c in codes if c["axis"] == "theme" and c["level"] == 2)
    n_action = sum(1 for c in codes if c["axis"] == "action")
    n_nonalert = [c["code"] for c in codes if not c["alertable"]]
    print(f"wrote {JSON_OUT}")
    print(f"  Section A: {n_theme_p} parent codes + {n_theme_s} sub-codes")
    print(f"  Section B: {n_action} action codes")
    print(f"  total scoreable classes: {len(codes)}")
    print(f"  reserved (unused): {', '.join(RESERVED_CODES)}")
    print(f"  flagged NOT alertable (measured sparsity): {', '.join(n_nonalert) or 'none'}")
    print("  validation: all invariants pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
