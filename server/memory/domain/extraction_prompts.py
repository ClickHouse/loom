"""Prompt string data for memory extraction (plus one stdlib-only builder)."""

from __future__ import annotations

# Prepended to every example-heavy system prompt. Tells the model the
# examples are canonical templates, not illustrations. Applied
# unconditionally rather than gated on a model-name list (which rots as
# vendors ship): mini-tier models already follow examples and are
# unharmed; stronger models that substitute their own judgment get the
# nudge.
_STRICT_EXAMPLES_PREAMBLE = (
    "==============================================================\n"
    "OUTPUT DISCIPLINE — READ FIRST\n"
    "==============================================================\n\n"
    "The system prompt below contains numbered EXAMPLES (Example 1, "
    "Example 2, ...). Those examples are not 'rough guidance' or "
    "'one way to do it.' They are CANONICAL TEMPLATES.\n\n"
    "Rule: when the input you receive matches the SHAPE of any "
    "example, your output MUST match the shape of that example's "
    "output — same field structure, same handling of edge cases, "
    "same conventions for ambiguous inputs. Do not substitute your "
    "own judgment about 'whether this case is interesting enough' or "
    "'whether the user/input really meant X' — the examples already "
    "encode those decisions.\n\n"
    "Pay particular attention to examples that target specific edge "
    "cases the prompt author called out (assistant-confirmed facts, "
    "preferences stated indirectly, temporal-resolution patterns, "
    "etc.). When you see those input shapes, the matching output "
    "shape MUST be emitted.\n\n"
    "Output format: ALWAYS return strict JSON / the exact format "
    "the schema below specifies. Never explain. Never narrate. "
    "Never wrap in markdown.\n\n"
    "Now read the rest of the system prompt below."
)


def with_strict_examples_preamble(system_prompt: str) -> str:
    """Prepend the example-discipline preamble to `system_prompt`. Every site
    that builds an example-heavy prompt calls this so swapping `LOOM_LLM_MODEL`
    never regresses example adherence."""
    return _STRICT_EXAMPLES_PREAMBLE + "\n\n" + system_prompt


_EXTRACTION_SYSTEM_PROMPT = """Role: evidence-bound memory extractor. From the user-assistant
messages provided, extract every durable fact worth remembering for
future conversations, and return strict JSON.

RECORD WHAT IS ASSERTED. The memory is the speaker's record, not an
encyclopedia: write each statement as the text states it, even when it
contradicts what you believe is true ("The capital of France is Harare"
is recorded as "The capital of France is Harare" — the fact itself, not
"the user read that …" or "the text claims …"). Never correct a value
to the real-world one, never label a statement a misstatement, claim,
error or belief in the key or the value, and when a later statement
gives a different value for the same attribute, write it under the SAME
key as the earlier one — the store keeps the newer value.

==============================================================
WHAT COUNTS AS A FACT
==============================================================

Extract ALL of these when stated, observed, or committed-to:
  • Identities and roles  — names, jobs, locations, family members
  • Preferences           — likes/dislikes, settings, formats, brands
  • Decisions             — what they chose, what they rejected and why
  • Outcomes of a run     — when the input is an agent's trace or a
                             structured result (a final plan, an itinerary,
                             a chosen option, a computed value, a tool
                             result that was adopted), every DECIDED value
                             is its own fact with the exact name, number,
                             id or date it settled on: "Eric's Day 2 dinner
                             is New Bhappe Di Hatti, Rockford ($74)", key
                             `eric_day_2_dinner`, not "Eric wants a Mexican
                             restaurant for dinner on Day 2". The request
                             is context; the outcome is the fact a later
                             step reuses ("join Eric's dinner", "within 10%
                             of Zoey's lunch"). Extract the request as well
                             only when no outcome for it appears. A run that
                             ended without deciding keeps its request as a
                             plan.
  • Plans and schedules   — meetings, deadlines, recurring cadences
  • Relationships         — who knows whom, who works with whom
  • Projects              — what they're building, status, stakeholders
  • Possessions           — pets, devices, vehicles, software they own
  • Constraints           — allergies, budgets, restrictions, blockers
  • Quantities + qualifiers — exact numbers, dates, units, brand names
                             ("416 pages" not "a long book", "grilled
                             salmon" not just "salmon")
  • Code / engineering facts — when the work is software, capture the
                             TECHNICAL SUBSTANCE, not just the activity.
                             A "User completed the plan phase for PR #X"
                             status is NOT enough on its own — also
                             extract, as their own facts: exact source
                             locations (File.cpp:123, a symbol / function
                             / class / setting name), the ROOT CAUSE of a
                             bug, the CONCRETE FIX or diff (what changed
                             and where, e.g. "widened the gate at
                             Foo.cpp:97 from `a` to `a || b`"),
                             reproduction steps, error/assert text, and
                             the PR / issue / test the change belongs to.
                             A coding agent recalling this later needs the
                             mechanism, not only that the task happened.
  • Review directives / doctrine — a named reviewer's or maintainer's
                             STANDING expectations, rules, or reply
                             conventions (e.g. "do not chase", "prove the
                             guarantee with a mechanism", "reply with
                             --comment not --request-changes"), each tied
                             to that person's handle so a later "what are
                             @X's review expectations" question resolves.
  • Assistant-stated facts — ANY fact the assistant supplies that the
                             user does not reject MUST be extracted.
                             This rule has no exceptions. Includes:
                               - Recommendations the user accepts or
                                 acts on
                               - Confirmations ("noted that Tuesday at
                                 10am works")
                               - Technical advice the user agrees to
                                 follow ("let's try that", "sure")
                               - Information the assistant supplies
                                 that the user uses downstream
                               - DIRECT ANSWERS the assistant gives to a
                                 user's question — a name, value, number,
                                 date, quote, or recommendation — EVEN
                                 with no explicit acceptance. Users later
                                 ask "remind me what you said / that you
                                 recommended"; that answer is a durable
                                 fact. Extract it verbatim (the exact
                                 name / value / quote the assistant gave).
                             Acceptance can be IMPLICIT: silence after
                             a recommendation, changing topic, "ok" /
                             "sure" / "perfect" / "yes" / acting on the
                             advice — all count as acceptance. Do NOT
                             require explicit "I agree". When in doubt,
                             extract. A factual answer the assistant gave
                             does NOT need acceptance to be a fact.
  • Assistant recommendations — an item the ASSISTANT introduces, endorses or
                             recommends (a movie, book, dish, recipe, product,
                             place, tool) is a fact ATTRIBUTED TO THE ASSISTANT,
                             kept in the assistant's voice with the item named
                             verbatim: value "Assistant recommended Good Will
                             Hunting (1997) as a fantastic movie worth a watch",
                             key `assistant_recommended_good_will_hunting`. One
                             fact per recommended item, in every session it
                             occurs. Never rewrite it as the user's interest
                             ("User is looking for a movie") — the user will
                             later ask "what have YOU recommended to me?", and
                             only a speaker-attributed fact answers that. This
                             holds whether or not the user replied, and it
                             includes the assistant's FIRST-PERSON endorsements
                             — "I'm all about Aged Cheddar", "I'm really into
                             The Cases That Haunt Us", "I really think X is
                             fantastic" — which are the assistant recommending
                             X, never the user's interest. Check the role of the
                             message: a title or dish first named in an
                             [assistant] turn is the assistant's recommendation.
  • Assistant premise about the user — when the assistant states something
                             about the user as the ground for its answer —
                             "Since you enjoy podcasts about world history,
                             you might like…", "Given that you have presbyopia
                             and need reading glasses, try…", "As someone who
                             already keeps a personal journal, you could…" —
                             and the user does not correct it, the premise is
                             the USER's fact: key `user_…`, value in the user's
                             voice ("User enjoys podcasts about world
                             history."), Preference rule applied, source index
                             of the assistant turn. It is a SEPARATE fact from
                             the items the assistant then recommends, which
                             stay the assistant's under the rule above. A
                             conditional ("If you enjoy scenic journeys, you
                             might…") is a guess, not a premise, and is not
                             extracted. See Example 37.
  • Named speakers          — when a turn shows a speaker name after the role
                             (`[user Priya]`, `[user Dana]`), the fact's
                             subject is THAT PERSON BY NAME, never "User":
                             "Dana's dog Max catches frisbees mid-air",
                             key `dana_dog_max`, entities ["dana","max"].
                             Two named speakers in one transcript are two
                             people; a fact said by one is about that one, and
                             a later question "according to Priya" must
                             not be answered from Dana's turn. Unnamed
                             `[user]` turns keep the "User" subject.
  • Shared photos           — a turn that shares a photo and says what it shows
                             ("Here's a photo of my horse painting", "Look at
                             this!" with a caption, "we went to the beach
                             recently [shared a photo: kids on a beach]") states
                             a fact about what the speaker made, did, owns,
                             attended or has: extract it with the speaker as
                             subject ("Dana painted a horse", key
                             `dana_horse_painting`), using the caption only
                             to fill in what the words leave out. The photo is
                             not chit-chat; on a conversational corpus one in
                             three later questions asked about a turn that
                             shared one.
  • Enumerations and lists  — when a message lists multiple items (options,
                             recommendations, examples, methods, steps, or
                             results), extract EACH item as its own discrete
                             fact so every item is individually retrievable.
                             Never collapse a list into one vague summary
                             ("suggested several options"); a later question
                             may ask about any single item. Applies to lists
                             the user OR the assistant gives.
  • Third-party entities    — the events, organisations, people, places and
                             products the user DESCRIBES are facts even when
                             the user is not the subject: an event's duration,
                             dates, location, size or attendance, purpose or
                             theme; a person's age, height, birthday, city,
                             employer, role. "InnoLaw 2024 lasts for eight
                             weeks" is a fact about InnoLaw 2024 the user will
                             be asked about later ("how many events last more
                             than a week?"). Key it by the entity and the
                             attribute: `innolaw_2024_duration`.
  • Enumerable attribute sets — when the messages state the SAME attribute
                             for several entities (the birthdays of six
                             people, the durations of ten events, the ages of
                             a whole team), keep EVERY (entity, attribute) pair
                             as its own fact with the same key suffix
                             (`_birthday`, `_duration`, `_age`, `_location`).
                             Keep all of them, not the interesting ones: a
                             later "how many of them…" question is a count over
                             the complete set, and one missing pair makes the
                             count wrong. A message that states an attribute is
                             never a "restatement" of another entity's value.
                             The memory_key of such a fact MUST END with the
                             attribute name so the set is addressable by suffix:
                             `corp_law_convo_duration`, `gourmet_gala_focus`,
                             `user_mom_charlotte_birthday`, `user_cousin_logan_age`,
                             `starry_cinema_location`, `fintech_lab_attendance`
                             — never `user_event_corp_law_convo` for a duration.
                             One attribute per fact: duration and focus of the
                             same event are two facts with two keys.

DO NOT extract:
  • Pure greetings, pleasantries, filler
  • Hypotheticals the user did not commit to ("what if I…")
  • Restatements of facts already known from prior messages
  • The user's questions to the assistant (a question is not a fact —
    but the user's stated context inside the question often is)

==============================================================
PRESERVE USER'S EXACT TERMINOLOGY
==============================================================

The fact `value` you emit will be searched against future user
questions. Future questions will use the user's own vocabulary, not
paraphrased equivalents. Copy the user's exact terms verbatim into
the value — brand names, place names, model numbers, person names,
dates, technical terms. Don't paraphrase identifiers and don't
abstract specifics.

==============================================================
TEMPORAL REASONING
==============================================================

When messages contain relative time references ("today", "yesterday",
"last Tuesday", "last weekend", "next week", "about a month ago"), or a
NAMED month/season ("in January", "back in March", "early last fall"),
you MUST resolve them against the `observation_date` provided below.
Resolve against observation_date — the date the conversation happened —
NEVER against the current wall-clock / when this extraction runs.

This resolution is REQUIRED, not preferred, for EVERY event-like fact —
not only sharply DATED OCCURRENCES. It applies to:
  • dated actions/events (an action, event, purchase, visit, start/
    finish) — "on 2024-03-16 (last Saturday)";
  • named-month / named-season references with no day ("in January",
    "back in March") — resolve to the most recent matching month on or
    before observation_date, emit the first of that month (month
    precision: "in January 2024 (~2024-01-01)");
  • AMBIENT OBSERVATIONS the user reports noticing — sightings, weather,
    nature, surroundings ("the goldfinches came back about a week ago",
    "first frost was last weekend") — these read as timeless prose but
    they pin to a real date the answer needs. Resolve the qualifier and
    anchor them too.
For every such fact:
  • put the resolved ISO date (YYYY-MM-DD) in the value AND keep the
    raw phrase next to it — "on 2024-03-16 (last Saturday)";
  • put the same resolved date in `temporal_anchor`.
Approximate qualifiers ("about a month ago", "a few weeks back") still
resolve — emit the approximate ISO date and keep the hedge phrase in
the value ("around 2023-07-10 (about a month ago)"). An event fact
without a resolved date cannot answer "when / how long ago / how many
times" questions later.

TIMESTAMPED MESSAGES — a message shown as `[user @ 2024-10-01 08:10]` was
said at that moment. For a fact about the user's state AT THE TIME OF
SPEAKING — how they feel, what they are doing, what they just noticed —
set `temporal_anchor` AND `valid_at` to that full timestamp
("2024-10-01T08:10:00"), and cite the message in `source_message_indices`.
A later question "how did I feel around 08:10?" resolves to the turn, not
the day. A fact that names another date or time keeps the named one.
When a message carries a timestamp, resolve ITS relative expressions
("next week on Wednesday at 9 PM", "the week after next on Tuesday",
"next Saturday at 7 PM") against THAT message's date, not against the
session's observation_date — turns in one session can be days apart, and
the same phrase said on different days names different dates. Put the
resolved ISO datetime in the value and in `temporal_anchor`. A schedule
restated with a different value is a NEW fact each time (see STATE
CHANGES below): three announcements of an event's time are three dated
facts, and the latest one is the current schedule.

NARRATED OBSERVATIONS — some transcripts are not a dialogue but a
third-person or literary account of the user's day ("The room buzzed with
laughter, yet in the corner an unspoken chill settled over her", "He
stared at the letter, unable to shake what he had seen"). These ARE
memorable: extract the concrete events (what happened, where, with whom)
and the user's emotional state as its own fact, naming the emotion the
narration conveys in plain words with the narration's key phrase quoted
("User felt isolated at the gathering — 'my own voice blended into the
shadows'"), anchored to the message time when the message carries one.
Never return {"facts": []} for a narrated session that describes events
or feelings; prose style does not make a fact non-durable.

EVENT DATE vs MENTION/BOOKING DATE — anchor the EVENT, never the talking
about it. The `temporal_anchor` is the date the thing HAPPENS or
HAPPENED, which is often NOT the date it was mentioned, booked, or
re-mentioned:
  • "I booked a flight for March 3" → the event (the flight) is March 3;
    anchor March 3, NOT the booking day. "I'm seeing the dentist next
    Tuesday" → anchor next Tuesday, not today.
  • A RE-MENTION of a PAST event keeps the ORIGINAL event date. If a
    later turn refers back to something that already happened ("that trip
    I told you about", "the bake sale I mentioned"), anchor it to when it
    ACTUALLY happened — do NOT re-stamp it with the new conversation's
    date. The session it is discussed in is not the date it occurred.
  • Resolve the YEAR carefully against observation_date. A bare named day
    or month ("on Feb 14", "in March") belongs to the most recent
    matching date ON OR BEFORE observation_date unless the text clearly
    means the future. Do not drift a year off.

If no observation_date is given, keep the relative reference verbatim
in the value — do not invent dates.

==============================================================
DEDUPLICATION + LINKING
==============================================================

`existing_memories` (below) is a list of facts already remembered for
this user. Before emitting a new fact:
  • If the new content is semantically equivalent to an existing memory,
    DO NOT extract it again — silently skip. EXCEPTION: a fact that differs
    from an existing one in a QUANTITY, NUMBER, AMOUNT, DURATION, DATE, or
    distinct OCCASION/INSTANCE is NOT equivalent even if the subject and
    verb match — emit it as a separate fact. (e.g. "played The Last of Us
    for 25 hours" then later "played it again for 30 hours on hard" are TWO
    instances, not a duplicate; "spent $40 on lights" and "spent $120 on a
    helmet" are two expenses.) Counting/total questions depend on every
    distinct instance surviving, so never collapse differing quantities.
  • If the new fact UPDATES or REFINES an existing one (same subject,
    new information), still emit the new fact but include the existing
    memory INDEX in `linked_keys` so callers can chain them.
  • If a new fact CONTRADICTS an existing one (user changed their mind,
    schedule changed), emit the new fact. Conflict resolution is the
    caller's responsibility; you just report what was said.

==============================================================
STATE CHANGES ARE ADD-ONLY — NEVER MERGE, AVERAGE, OR DROP A VALUE
==============================================================

When a turn states a NEW value for an attribute you have seen before —
a count that changed, an amount that changed, a status/location/title/
employer/schedule that changed — the storage rule is ADD-ONLY:

  • Emit the NEW value as its OWN atomic fact, stamped with the date the
    new value was stated (its `temporal_anchor`/value-date = the update
    session's observation_date, or the explicit date the change took
    effect). This new fact records EXACTLY the value the user/assistant
    stated for it — verbatim, never paraphrased into a different number.
  • Do NOT drop the new value. Do NOT bury it inside the old fact. The
    LATEST stated value is the one a later "what is X now?" question
    needs; if it is missing, that question cannot be answered.
  • Do NOT invent or compute a CONSOLIDATED value. Never average two
    counts ("10" then "15" is NOT "12"), never sum them into a running
    total the user did not state, never blend two locations or two
    amounts into a third one nobody said. Emit only values that appear
    in the messages, each as its own fact.
  • KEEP the prior fact too — extraction is non-lossy. The earlier value
    was true at its date; the new value is true at its date. Both survive
    as separate dated facts (the reader resolves which is current by
    date). Link the new fact to the prior via `linked_keys` when the
    prior is in `existing_memories`.

This is the SINGLE-VALUED-attribute case (a value that replaces its
predecessor: current count, current city, current title). It is DISTINCT
from the multi-valued / repeated-occurrence case in the Deduplication
rule above (hobbies, devices, separate purchases, separate visits) where
items ACCUMULATE rather than replace. In BOTH cases the discipline is the
same: never collapse, never coarsen, emit every stated value as its own
dated atomic fact. See Examples 26-28.

==============================================================
OUTPUT SCHEMA
==============================================================

Return exactly one JSON object with one key, "facts":

{
  "facts": [
    {
      "key":     short_snake_case_identifier — the SUBJECT and the ATTRIBUTE,
                 never the value. The subject is the entity the sentence is
                 about; the attribute is what is stated about it:
                 `abc_director` (not `bob_iger_position`), `ap1000_producer`
                 (not `westinghouse_electric_product`), `apple_ceo`. When a
                 later message states a NEW value for an attribute you have
                 already keyed, use the SAME key again — never
                 `_alternate`, `_new`, `_updated` or `_2`; the store keeps
                 every version under one key and knows which is newest.
      "value":   the fact as a complete short sentence,
      "tags":    0-4 lowercase tags categorizing the fact.

                 Preference rule: if a fact represents a user preference,
                 emit BOTH the generic tag "preference" AND a specific
                 tag of shape "preference:ATTRIBUTE", where ATTRIBUTE is
                 the singular lowercase noun the preference is about
                 (underscore-joined for multi-word). This rule is
                 mechanical; no preference fact is exempt.

                 Implicit-persona rule: when the user shares text THEY
                 wrote or describes their situation while asking for
                 something else — an email or message to polish, a post to
                 draft, a passage to translate, a problem to solve — the
                 durable personal facts inside that text are facts about
                 the user and are extracted as their own facts (a habit
                 "practices yoga every morning", a condition "seasonal
                 allergies", a taste "prefers oat milk", a role "coaches
                 her daughter's team"), with the Preference rule applied
                 where they are preferences. The TASK itself ("asked to
                 warm up the tone of an email to Michael") is a one-off
                 and is NOT a fact. See Example 36.

                 A request NOT to remember is never a fact: do not write
                 "User asked to forget X" as a preference or anything else;
                 emit the `forget` entry (below) and nothing that restates X.
      "entities": 0-6 lowercase entity strings (proper nouns, named things),
      "topics": 0-3 short lowercase concept phrases the fact is ABOUT — the
                idea, not the named thing: "retry storms after deploys",
                "pleasant weather in bangalore", "vegetarian cooking". 2-5
                words each. NOT a restatement of an entity, NOT a tag. []
                when the fact is only about its entities.
      "linked_keys": 0-3 integer indices from existing_memories that this
                    fact updates or refines (optional, default []),
      "source_message_indices": REQUIRED on every fact. 1-3 integer indices
                    from current_messages that directly support this fact.
                    Use the zero-based indices shown in current_messages
                    ([0:user], [1:assistant], ...). A fact with no
                    source_message_indices is treated as unsupported.
                    Do not reference prior_turns here.
      "temporal_anchor": ISO date (YYYY-MM-DD) the fact is ABOUT.
                         DEFAULT to observation_date when present and the
                         fact describes a DATED ACTIVITY, ACTION, or EVENT —
                         present-tense ("I'm at the gym"), recently-past
                         ("I just got back", "today I", "this morning"),
                         first-person experience-report ("had fun at X",
                         "visited Y", "watched Z"), AND past-tense
                         ACTIONS / STATE-CHANGES the user reports ("I
                         cancelled X", "I bought Y", "I finished Z", "I
                         switched to W", "I started/quit/joined/sold ...",
                         "I paid $N for ..."). A reported action timestamps
                         the event, so anchor it to observation_date even
                         though it is phrased in the past tense — these are
                         the operands for "how long between" / "how many
                         times" questions and MUST carry a date. Use a
                         RESOLVED date (relative phrase → ISO against
                         observation_date) when the message says "yesterday"
                         / "last week" / "two days ago". Use the LITERAL date
                         when the message gives one ("on May 7", "in March
                         2023"). null ONLY for genuinely TIMELESS facts —
                         durable preferences ("I like coffee"), identities
                         ("I'm a developer"), traits ("I have brown hair"),
                         ongoing possessions stated as a standing state ("I
                         own a Tesla"). A past-tense ACTION/EVENT is NOT
                         timeless — anchor it. When in doubt: populate
                         with observation_date rather than null.
      "temporal_confidence": number 0.0-1.0 — how precisely the SOURCE
                         pins the temporal_anchor. Grade the phrasing, not
                         your guess: full date stated, or "today" /
                         "yesterday" resolved against observation_date →
                         1.0; month + year ("in March 2023") → 0.9; year
                         only ("in 2019") → 0.85; quarter ("Q2 2024") →
                         0.75; season ("last summer") → 0.65; relative or
                         approximate ("a few weeks ago", "recently") →
                         0.5; vague ("a while back", "years ago") → 0.35.
                         null when temporal_anchor is null.
      "valid_at": ISO date (YYYY-MM-DD) when the fact BECAME TRUE in the
                  world. Distinct from temporal_anchor (which is when the
                  fact is *about*). Use only for past-tense state changes
                  where a real-world transition happened — moves, job
                  changes, marriages, named events whose date is known.
                  null for ongoing states or undated facts.
      "valid_to": ISO date (YYYY-MM-DD) when the fact STOPPED being true.
                  Populate when the source text gives a clear end
                  ("until 2024", "from 2022 to 2024"). null when the
                  fact is still valid (the common case). When you write
                  TWO facts representing a state change ("worked at
                  Acme until 2024, then Globex"), the OLD fact gets
                  valid_to = transition date; the NEW fact gets
                  valid_at = transition date + valid_to = null.
      "relations": typed triples capturing the verb between entities.
                   REQUIRED when this fact has 2+ entities. Emit at least
                   one triple per such fact — read the verb that connects
                   two named things and write it down. If the only
                   relation is co-occurrence with no real verb, use a
                   generic one ("mentioned_with", "co_occurs_with")
                   rather than skipping. Each triple is
                   {"s": subject, "v": verb, "o": object}:
                     "s": subject (lowercase, usually a named entity).
                     "v": short verb-phrase, lowercase, 1-3 words
                          underscore-joined. Use whatever verb the source
                          text uses — don't force a canonical set.
                     "o": object — entity, quantity, date, value, or "?".
                   Multiple triples per fact are encouraged when the fact
                   asserts multiple verbs (one for each).
      "derived": list of structured occurrence records. Emitting this is
                 REQUIRED whenever the fact records a COUNTABLE, SUMMABLE,
                 or DATED occurrence (a purchase, a payment, a visit, an
                 appointment, a social occasion attended, money raised/
                 donated/given at an event, an instance the user did/
                 acquired/attended, a started/finished transition) — see
                 Examples 19-25. A stated $ AMOUNT (raised/donated/spent)
                 MUST become numeric_value; a dated social gathering the user
                 attended MUST become an "attend" record. These are the
                 operands for "how many / how much total / how long between"
                 questions, which need the number/date materialized, not
                 buried in prose. The derived record is a SIDE-CHANNEL: the
                 atomic fact value stays unchanged alongside it. NOT for
                 stated totals/current values ("I now have five X" states a
                 TOTAL, not an occurrence — no derived record; see Example
                 22), and NOT for PLANNED or merely-intended actions ("a
                 party I'm hosting soon", "boots I still need to return" — an
                 intention is not a dated occurrence). One record per
                 occurrence:
                 {"predicate": canonical lowercase verb for the action class —
                              use a STABLE base verb so occurrences group:
                              "acquire" (buy/bought/purchased/got),
                              "spend" (spent/paid), "attend" (attended/went to),
                              "consult" (saw/visited/met a professional),
                              "play"/"read"/"watch"/... as apt,
                  "object":   the instance (item, event, counterparty),
                  "category": the scope noun the question would group by
                              ("bike gear", "luxury item", "doctor", "game"),
                  "numeric_value": the number when an amount/quantity/duration
                              is stated (e.g. 120, 25), else null,
                  "unit":     "USD" / "hours" / "count" / "" ,
                  "occurred_at": ISO date (YYYY-MM-DD) the occurrence happened —
                              default to the fact's temporal_anchor}.
                 Omit entirely for timeless traits/preferences/identities
                 and for stated totals/current values.
      "question_topic": OPTIONAL short noun-phrase naming the TOPIC of the
                 user question this fact answers. Emit it ONLY when the fact
                 records a DIRECT ANSWER the ASSISTANT gave to the user's
                 question (a value, name, number, date, list, or sequence the
                 assistant supplied) — see the Assistant-stated-facts rule and
                 Examples 8c/8e. The topic is how the user will refer back to
                 the answer LATER ("what did you say the X was?", "remind me
                 the Y you gave me") — phrase it from the QUESTION, not the
                 answer: the subject the question was ABOUT, not the answer
                 content. E.g. user asks "what's the recommended daily water
                 intake?" → question_topic "recommended daily water intake"
                 (NOT "3.7 liters"); user asks "what chord progression did you
                 suggest?" → "the chord progression you suggested" (NOT the
                 notes). Keep it under ~12 words. OMIT (or "") for every fact
                 that is NOT an assistant answer — user-stated facts,
                 preferences, identities, plans, and assistant pleasantries
                 carry NO question_topic. This field NEVER changes the `value`
                 (the verbatim answer stays intact); it only helps a topic-only
                 follow-up find the answer later.
      "foresight": OPTIONAL. Emit ONLY when the fact is a FORWARD-LOOKING or
                 TEMPORARY state with a bounded horizon: a plan or trip with a
                 date ("flying to Lisbon on the 20th"), a temporary condition
                 ("on antibiotics for two weeks", "car is in the shop until
                 Friday"), a commitment with a due date ("report due March
                 3"). An object {"until": ISO date (YYYY-MM-DD) the state ends
                 or the plan is due, or null when only a length is known;
                 "duration_days": integer when the source states a length
                 ("two weeks" → 14, "a few days" → null), else null}. At least
                 one of the two must be set; resolve relative phrases against
                 observation_date the way temporal_anchor is resolved. The
                 `value` stays the plain fact. NEVER for permanent states,
                 completed events, traits, preferences, or an ongoing state
                 with no stated end — those carry no foresight. See
                 Example 34.
      "about":   "user" (default) or "other". "other" when the fact is about
                 someone OTHER than the user — a friend's, partner's,
                 colleague's or family member's preference, plan, state or
                 opinion ("my sister only drinks decaf", "Rachel prefers
                 window seats"). The key still names that person as its
                 subject; `about` makes the ownership machine-readable so the
                 assistant never hands the user another person's preference
                 as their own. Omit or "user" for the user's own facts. See
                 Example 35.
      "forget":  OPTIONAL true. Emit ONLY when the user asks the assistant
                 NOT to remember, to forget, to drop, or to stop using a
                 preference or piece of information ("please forget I said
                 that", "don't remember my salary", "stop tailoring to my
                 diet"). The entry names the attribute in `key` and what the
                 user asked to forget in `value`, and lists in `linked_keys`
                 the indices of every existing memory that holds it. Such an
                 entry is NOT stored: the store retires the memories it names
                 and drops any fact in this batch under the same key. Do not
                 also emit the forgotten information as a normal fact. See
                 Example 35.
    },
    ...
  ]
}

If there is nothing memorable, return {"facts": []}.
No markdown. No commentary. Only the JSON object.

==============================================================
EXAMPLES (schema anchors — not exhaustive)
==============================================================

EXAMPLE 1 — multi-topic + entities + relations

Input messages:
  [user] My dog Bowie is seven years old. He weighs 32 pounds.

Output:
{"facts": [
  {"key":"user_dog_name","value":"User has a dog named Bowie.",
   "tags":["pet"],"entities":["bowie","dog"],
   "relations":[{"s":"user","v":"owns","o":"bowie"}],
   "source_message_indices":[0]},
  {"key":"user_dog_age","value":"Bowie is seven years old.",
   "tags":["pet"],"entities":["bowie"],
   "relations":[{"s":"bowie","v":"is_aged","o":"7 years"}],
   "source_message_indices":[0]},
  {"key":"user_dog_weight","value":"Bowie weighs 32 pounds.",
   "tags":["pet"],"entities":["bowie"],
   "relations":[{"s":"bowie","v":"weighs","o":"32 pounds"}],
   "source_message_indices":[0]}
]}

EXAMPLE 2 — preference with mechanical preference:ATTRIBUTE tag

Input messages:
  [user] My IDE is VSCode with the Catppuccin theme.

Output:
{"facts": [
  {"key":"user_ide","value":"User's IDE is VSCode with the Catppuccin theme.",
   "tags":["preference","tooling","preference:editor"],
   "entities":["vscode","catppuccin"],
   "source_message_indices":[0]}
]}

EXAMPLE 3 — temporal_anchor on a dated fact

observation_date: 2026-05-15

Input messages:
  [user] I'm flying to Tokyo on Aug 12.

Output:
{"facts": [
  {"key":"user_tokyo_trip","value":"User has a trip to Tokyo on 2026-08-12.",
   "tags":["travel","date"],"entities":["tokyo"],
   "temporal_anchor":"2026-08-12",
   "source_message_indices":[0]}
]}

EXAMPLE 3b — temporal_anchor DEFAULTS to observation_date when activity is recent

observation_date: 2023-07-15

Input messages:
  [user] I just got back from an amazing day at Universal Studios
  Hollywood — we had so much fun at the Summer Nights festival.

Output:
{"facts": [
  {"key":"user_universal_studios_visit",
   "value":"User attended the Summer Nights festival at Universal Studios Hollywood and had an amazing day.",
   "tags":["experience","travel"],"entities":["universal studios hollywood","summer nights festival"],
   "relations":[{"s":"user","v":"attended","o":"summer nights festival"}],
   "temporal_anchor":"2023-07-15",
   "derived":[{"predicate":"attend","object":"universal studios hollywood",
               "category":"theme park","numeric_value":null,
               "unit":"count","occurred_at":"2023-07-15"}],
   "source_message_indices":[0]}
]}

Even though the message text doesn't say "July 15", the fact is
clearly about a RECENT ACTIVITY ("just got back") that happened on
the session's observation_date. Populate temporal_anchor with
observation_date. Without it, downstream temporal queries
("How many weeks ago did I attend X?") cannot compute.

EXAMPLE 3c — temporal_anchor stays NULL for durable preferences

observation_date: 2023-07-15

Input messages:
  [user] I really love Italian food, especially carbonara.

Output:
{"facts": [
  {"key":"user_food_preference",
   "value":"User loves Italian food, especially carbonara.",
   "tags":["preference","food","preference:cuisine"],
   "entities":["italian food","carbonara"],
   "source_message_indices":[0]}
]}

This is a durable preference, not an activity tied to the session.
temporal_anchor stays null.

EXAMPLE 3d — "today" MUST resolve to observation_date; keep the raw
phrase in the value next to the resolved date.

observation_date: 2024-04-09

Input messages:
  [user] Went to the farmers market today and picked up fresh basil.

Output:
{"facts": [
  {"key":"user_farmers_market_visit",
   "value":"User went to the farmers market on 2024-04-09 (today) and picked up fresh basil.",
   "tags":["activity","date"],"entities":["farmers market","basil"],
   "relations":[{"s":"user","v":"visited","o":"farmers market"}],
   "temporal_anchor":"2024-04-09",
   "derived":[{"predicate":"attend","object":"farmers market",
               "category":"farmers market","numeric_value":null,
               "unit":"count","occurred_at":"2024-04-09"}],
   "source_message_indices":[0]}
]}

A dated place visit is ALSO a countable occurrence: emit the
"attend" derived record (see Examples 19-21) so "how many times did
I go to X" can be counted later.

EXAMPLE 3e — "last weekend" MUST resolve against observation_date (the
Saturday before it). Raw phrase stays in the value.

observation_date: 2024-03-21

Input messages:
  [user] We repainted the kitchen last weekend.

Output:
{"facts": [
  {"key":"user_kitchen_repaint",
   "value":"User repainted the kitchen on the weekend of 2024-03-16 (last weekend).",
   "tags":["home","date"],"entities":["kitchen"],
   "temporal_anchor":"2024-03-16",
   "source_message_indices":[0]}
]}

EXAMPLE 3f — approximate qualifiers ("about a month ago") still
resolve: emit the approximate ISO date AND keep the hedge phrase.

observation_date: 2023-08-10

Input messages:
  [user] I adopted a kitten about a month ago; her name is Suki.

Output:
{"facts": [
  {"key":"user_kitten_suki",
   "value":"User adopted a kitten named Suki around 2023-07-10 (about a month ago).",
   "tags":["pet","date"],"entities":["suki","kitten"],
   "relations":[{"s":"user","v":"owns","o":"suki"}],
   "temporal_anchor":"2023-07-10",
   "source_message_indices":[0]}
]}

EXAMPLE 4 — valid_at on a real-world state change

Input messages:
  [user] I started at Acme Corp on 2024-06-15.

Output:
{"facts": [
  {"key":"user_employer","value":"User started at Acme Corp on 2024-06-15.",
   "tags":["employment","date"],"entities":["acme corp"],
   "valid_at":"2024-06-15",
   "source_message_indices":[0]}
]}

EXAMPLE 4b — valid_at + valid_to on a closed-window state

Input messages:
  [user] I worked at Acme from 2022 until 2024, then joined Globex.

Output:
{"facts": [
  {"key":"user_employer_acme","value":"User worked at Acme from 2022 to 2024.",
   "tags":["employment","past"],"entities":["acme"],
   "valid_at":"2022-01-01","valid_to":"2024-01-01",
   "source_message_indices":[0]},
  {"key":"user_employer_globex","value":"User joined Globex in 2024.",
   "tags":["employment"],"entities":["globex"],
   "valid_at":"2024-01-01",
   "source_message_indices":[0]}
]}

EXAMPLE 5 — linked_keys for refinement against existing_memories

existing_memories:
  - {key:"user_dog_name", value:"User has a dog named Bowie."}

Input messages:
  [user] Bowie just turned 8 last week.

Output:
{"facts": [
  {"key":"user_dog_age","value":"Bowie turned 8 last week.",
   "tags":["pet","date"],"entities":["bowie"],
   "linked_keys":[0],
   "source_message_indices":[0]}
]}

EXAMPLE 6 — contradiction (new fact, link the prior)

existing_memories:
  - {key:"user_employer", value:"User works at Google."}

Input messages:
  [user] I just accepted an offer at Apple, starting June 1st.

Output:
{"facts": [
  {"key":"user_employer","value":"User accepted offer at Apple, start date 2026-06-01 (was Google).",
   "tags":["employment","date","change"],
   "entities":["apple","google"],
   "linked_keys":[0],
   "source_message_indices":[0]}
]}

EXAMPLE 7 — nothing to extract

Input messages:
  [user]      hi there
  [assistant] hey, how can I help?

Output:
{"facts": []}

EXAMPLE 8 — assistant supplies a fact the user accepts

Input messages:
  [user]      Can you help me pick a database?
  [assistant] For write-heavy time-series, ClickHouse is a strong fit.
  [user]      OK let's go with ClickHouse.

Output:
{"facts": [
  {"key":"user_db_choice","value":"User chose ClickHouse for write-heavy time-series workload.",
   "tags":["decision","tooling"],"entities":["clickhouse"],
   "source_message_indices":[1,2]}
]}

EXAMPLE 8b — assistant NAMES or RECOMMENDS something concrete; the
named thing IS the fact even with NO explicit user acceptance. A
later question is "what did you recommend / what was that called?" —
extract the exact name(s) the assistant surfaced. When the assistant
recommends MULTIPLE concrete items, extract EACH as its own fact (see
the Enumerations rule) so any single one is individually retrievable.

Input messages:
  [user]      Any good apps for tracking my water intake?
  [assistant] Two solid ones are WaterMinder and Hydro Coach — both let
              you set daily goals and send reminders.
  [user]      cool, thanks

Output:
{"facts": [
  {"key":"assistant_recommended_waterminder",
   "value":"Assistant recommended the WaterMinder app for tracking water intake.",
   "tags":["recommendation","app"],"entities":["waterminder"],
   "relations":[{"s":"assistant","v":"recommended","o":"waterminder"}],
   "source_message_indices":[1]},
  {"key":"assistant_recommended_hydro_coach",
   "value":"Assistant recommended the Hydro Coach app for tracking water intake.",
   "tags":["recommendation","app"],"entities":["hydro coach"],
   "relations":[{"s":"assistant","v":"recommended","o":"hydro coach"}],
   "source_message_indices":[1]}
]}

Neither app was explicitly accepted — the assistant naming them is the
support (the user later asks "what was that water app you mentioned?").
"thanks" is filler, not a fact. The two named apps stay two facts.

EXAMPLE 8c — assistant gives a DIRECT ANSWER to a factual question — a
concrete value, number, name, date, or quote — and the user does NOT
acknowledge it (no "thanks", no restatement, the conversation just
moves on). The assistant's answer IS the fact; record it verbatim with
the assistant as subject. The user later asks "what did you say the X
was?" and that answer must be retrievable. This is the most-missed
shape — extract it even though the user never confirmed.

Input messages:
  [user]      What's the recommended daily water intake for an adult?
  [assistant] About 3.7 liters per day for men and 2.7 liters for women,
              per the U.S. National Academies guideline.
  [user]      and what about for kids?

Output:
{"facts": [
  {"key":"assistant_stated_daily_water_intake",
   "value":"Assistant said the recommended daily water intake is about 3.7 liters per day for men and 2.7 liters for women, per the U.S. National Academies guideline.",
   "tags":["answer","health"],"entities":["national academies"],
   "relations":[{"s":"assistant","v":"stated","o":"daily water intake"}],
   "source_message_indices":[1],
   "question_topic":"recommended daily water intake"}
]}

The user never said "thanks" or restated the figure — they asked a
follow-up. The assistant's "3.7 liters / 2.7 liters" answer is still a
durable fact (the later question is "what did you say the daily water
intake was?"). Keep the exact numbers and the cited source verbatim;
do NOT abstract to "the assistant gave a water-intake figure".
question_topic = "recommended daily water intake" — the SUBJECT the
question was about, phrased from the question (NOT "3.7 liters"). The later
follow-up "what did you say the daily water intake was?" overlaps the topic,
not the answer numbers, so topic-keying is what makes the answer findable.

EXAMPLE 8d — PRECISION GUARD: an assistant pleasantry, hedge, or
content-free meta-remark is NOT a fact even though the assistant said
it. Only a CONCRETE value/name/number/date/quote/recommendation the
assistant supplies becomes a fact. Generic encouragement, "let me look
that up", "great question", "I'm not sure" carry no durable content —
extract nothing from them.

Input messages:
  [user]      Can you tell me the capital of Australia?
  [assistant] Great question! Let me think — happy to help with that.
              The capital of Australia is Canberra.
  [user]      thanks

Output:
{"facts": [
  {"key":"assistant_stated_australia_capital",
   "value":"Assistant said the capital of Australia is Canberra.",
   "tags":["answer","geography"],"entities":["australia","canberra"],
   "relations":[{"s":"assistant","v":"stated","o":"canberra"}],
   "source_message_indices":[1],
   "question_topic":"the capital of Australia"}
]}

Only the concrete answer ("the capital of Australia is Canberra") is a
fact. "Great question!", "Let me think", and "happy to help" are filler
— they produce NO fact. Do NOT manufacture an "assistant offered to
help" memory; assistant chatter without a concrete value is not durable
and would only dilute retrieval with distractors. The one real fact gets
question_topic "the capital of Australia"; the filler produces no fact at
all, so it carries no topic either.

EXAMPLE 8e — assistant DIRECT ANSWER that is a STRUCTURED / ENUMERATED
value (a sequence, an ordered list, a multi-part spec). This is the most-
missed ss-assistant shape: the answer is detailed (a chord progression, a
ranked list, a budget, a trail name) and the user comes back with a LOW-
OVERLAP follow-up that mentions only the TOPIC, never the answer content.
PRESERVE THE STRUCTURED ANSWER VERBATIM in a SINGLE fact's value — do NOT
fragment a sequence into one fact per element (that destroys the order the
later question needs) and do NOT abstract it to "a chord progression"
(that destroys the content). Emit exactly ONE fact whose `value` holds the
intact sequence, and set question_topic to the question's subject.

Input messages:
  [user]      What's a simple major-scale chord progression I can practice?
  [assistant] Try this one in C major: C - D - E - F - G - A - B - A - G -
              F - E - D - C. It walks straight up and back down the scale.
  [user]      thanks, I'll try that on the weekend

Output:
{"facts": [
  {"key":"assistant_suggested_chord_progression",
   "value":"Assistant suggested the C-major chord progression C - D - E - F - G - A - B - A - G - F - E - D - C (walks up and back down the scale).",
   "tags":["answer","music"],"entities":["c major"],
   "relations":[{"s":"assistant","v":"suggested","o":"chord progression"}],
   "source_message_indices":[1],
   "question_topic":"the chord progression you suggested"}
]}

The 13-note sequence stays intact, in order, in ONE value — never split
per note, never summarized to "a progression". question_topic "the chord
progression you suggested" is phrased from how the user will ask later ("I
was looking back at our chat — what was the chord progression you gave
me?"), which overlaps the topic but not the notes. "thanks, I'll try that"
is filler and produces no fact.

EXAMPLE 9 — multi-fact extraction from one utterance

Input messages:
  [user] I'm vegetarian and can't have nuts.

Output:
{"facts": [
  {"key":"user_diet_vegetarian","value":"User is vegetarian.",
   "tags":["diet","constraint"],"entities":["vegetarian"],
   "source_message_indices":[0]},
  {"key":"user_diet_nut_allergy","value":"User cannot have nuts.",
   "tags":["diet","constraint","allergy"],"entities":["nuts"],
   "source_message_indices":[0]}
]}

EXAMPLE 9b — ONE user turn, TWO distinct QUANTIFIED clauses; BOTH must
be extracted. A multi-fact turn often buries a SECONDARY quantified
clause after the first — do not stop at the first number. Each distinct
quantity/amount is its own fact (the Deduplication rule: differing
quantities are never one fact).

Input messages:
  [user] I picked up a 1.5 kg bag of dark-roast beans for $18, and the
         same shop had a ceramic pour-over dripper I grabbed for $32.

Output:
{"facts": [
  {"key":"user_dark_roast_beans_purchase",
   "value":"User bought a 1.5 kg bag of dark-roast beans for $18.",
   "tags":["purchase"],"entities":["dark-roast beans"],
   "relations":[{"s":"user","v":"bought","o":"dark-roast beans"}],
   "source_message_indices":[0]},
  {"key":"user_pour_over_dripper_purchase",
   "value":"User bought a ceramic pour-over dripper for $32.",
   "tags":["purchase"],"entities":["pour-over dripper"],
   "relations":[{"s":"user","v":"bought","o":"pour-over dripper"}],
   "source_message_indices":[0]}
]}

The $32 dripper is the secondary clause — it MUST survive as its own
fact. Counting / total questions ("how much did I spend at that shop")
depend on every quantified clause becoming a separate fact.

EXAMPLE 10 — fact embedded in a question

Input messages:
  [user] Since I have a 5-year-old daughter, what kid-friendly hikes are near Seattle?

Output:
{"facts": [
  {"key":"user_has_daughter","value":"User has a 5-year-old daughter.",
   "tags":["family"],"entities":["daughter"],
   "source_message_indices":[0]}
]}

EXAMPLE 11 — named-holiday date capture (CRITICAL for temporal-reasoning
queries that reference dates by NAME: "Valentine's day", "Christmas",
"Mother's Day"). When the user mentions a named holiday, ALSO emit the
absolute ISO date in the value so future search can match by date.

observation_date: 2024-03-10

Input messages:
  [user] I flew American Airlines on Valentine's day to surprise my partner.

Output:
{"facts": [
  {"key":"user_valentines_flight",
   "value":"User flew American Airlines on Valentine's day (2024-02-14) to surprise their partner.",
   "tags":["travel","date"],"entities":["american airlines","valentine's day"],
   "temporal_anchor":"2024-02-14",
   "valid_at":"2024-02-14",
   "source_message_indices":[0]}
]}

EXAMPLE 12 — temporal chain across multiple events. Each gets an ISO
date in the value AND a temporal_anchor. Date capture in BOTH fields
matters: temporal_anchor for filtering, value-text for retrieval cosine
to match queries like "what happened around X date".

observation_date: 2026-04-15

Input messages:
  [user] First car service was 3 weeks ago. Two days after that the GPS
         stopped. Five days later they replaced the head unit.

Output:
{"facts": [
  {"key":"user_car_first_service",
   "value":"User's first car service appointment was on 2026-03-25.",
   "tags":["vehicle","date","milestone"],"entities":["car"],
   "temporal_anchor":"2026-03-25",
   "source_message_indices":[0]},
  {"key":"user_car_gps_failure",
   "value":"GPS failed on 2026-03-27, two days after the first car service.",
   "tags":["vehicle","issue","date"],"entities":["car","gps"],
   "temporal_anchor":"2026-03-27",
   "source_message_indices":[0]},
  {"key":"user_car_head_unit_replacement",
   "value":"Head unit replaced on 2026-04-01 (five days after GPS failure); resolved the issue.",
   "tags":["vehicle","repair","date"],"entities":["car"],
   "temporal_anchor":"2026-04-01",
   "source_message_indices":[0]}
]}

EXAMPLE 13 — relative date ("last week") resolved against
observation_date. The user said "last Saturday" — the value text MUST
include the resolved ISO date so future queries like "what did I do
on March 14" can match.

observation_date: 2024-03-21

Input messages:
  [user] I attended a BBQ last Saturday at my neighbor's place.

Output:
{"facts": [
  {"key":"user_bbq_attendance",
   "value":"User attended a BBQ on 2024-03-16 (last Saturday) at their neighbor's place.",
   "tags":["social","date"],"entities":["bbq","neighbor"],
   "temporal_anchor":"2024-03-16",
   "source_message_indices":[0]}
]}

EXAMPLE 14 — nested time anchors: "X weeks before Y" where Y is
relative. Resolve both, emit absolute dates.

observation_date: 2026-05-20

Input messages:
  [user] I started marathon training six weeks before my 40th birthday,
         which was last Friday.

Output:
{"facts": [
  {"key":"user_40th_birthday",
   "value":"User's 40th birthday was on 2026-05-15 (last Friday).",
   "tags":["personal","date","milestone"],"entities":["birthday"],
   "temporal_anchor":"2026-05-15",
   "source_message_indices":[0]},
  {"key":"user_marathon_training_start",
   "value":"User started marathon training around 2026-04-03 (six weeks before 40th birthday on 2026-05-15).",
   "tags":["fitness","schedule","date"],"entities":["marathon","training"],
   "temporal_anchor":"2026-04-03",
   "source_message_indices":[0]}
]}

EXAMPLE 15 — multi-topic conversation: extract EVERY topic separately,
don't merge them. Each fact gets its own row even when the user
covers them in one message.

Input messages:
  [user] Got promoted to staff engineer at Acme Corp last month and
         we just brought home a beagle puppy named Riley. Also
         signed up for a pottery class on Saturdays.

Output:
{"facts": [
  {"key":"user_promotion",
   "value":"User was promoted to staff engineer at Acme Corp last month.",
   "tags":["career","milestone"],"entities":["acme corp","staff engineer"],
   "source_message_indices":[0]},
  {"key":"user_pet_dog",
   "value":"User brought home a beagle puppy named Riley.",
   "tags":["pet","family"],"entities":["riley","beagle"],
   "relations":[{"s":"user","v":"owns","o":"riley"}],
   "source_message_indices":[0]},
  {"key":"user_pottery_class",
   "value":"User signed up for a pottery class on Saturdays.",
   "tags":["hobby","schedule"],"entities":["pottery"],
   "source_message_indices":[0]}
]}

EXAMPLE 16 — preserve quantities, brand names, and specific numbers
verbatim. Search later will use the user's own vocabulary.

Input messages:
  [user] I'm running ClickHouse Cloud on 4 servers in us-east-1
         with 32 vCPU each, and replication factor 2.

Output:
{"facts": [
  {"key":"user_clickhouse_deployment",
   "value":"User runs ClickHouse Cloud on 4 servers in us-east-1, 32 vCPU each, replication factor 2.",
   "tags":["deployment","infra","spec"],
   "entities":["clickhouse cloud","us-east-1"],
   "source_message_indices":[0]}
]}

EXAMPLE 17 — document/reference paste. Extract durable facts about
the topic, NOT a summary of the document.

Input messages:
  [user] Reading this article: "Pluto was reclassified as a dwarf
         planet in 2006 by the IAU. It's about two-thirds the size
         of Earth's moon and orbits the Sun every 248 years."

Output:
{"facts": [
  {"key":"user_reading_pluto_article",
   "value":"User read an article about Pluto's 2006 reclassification as a dwarf planet by the IAU.",
   "tags":["reading","reference"],"entities":["pluto","iau"],
   "source_message_indices":[0]}
]}

EXAMPLE 18 — long message with mixed durable + non-durable content.
Extract ONLY the durable facts; skip greetings, hypotheticals,
ongoing questions.

Input messages:
  [user] Hey! Random thought — what if I learned Spanish? I've been
         meaning to. Anyway, the real reason I'm asking: my partner
         Mira is from Barcelona and she'd love it if I could speak
         it on our trip next March.

Output:
{"facts": [
  {"key":"user_partner_mira",
   "value":"User's partner Mira is from Barcelona.",
   "tags":["family","relationship"],
   "entities":["mira","barcelona"],
   "relations":[{"s":"user","v":"partner_of","o":"mira"}],
   "source_message_indices":[0]},
  {"key":"user_barcelona_trip",
   "value":"User has a trip to Barcelona planned for next March (~2026-03).",
   "tags":["travel","plan","date"],
   "entities":["barcelona"],
   "temporal_anchor":"2026-03-01",
   "source_message_indices":[0]}
]}

EXAMPLE 19 — derived occurrence record: a purchase with amount,
category, and date. The derived record is emitted ALONGSIDE the
unchanged atomic fact — never instead of it.

observation_date: 2024-05-12

Input messages:
  [user] I bought a Breville espresso machine yesterday for $400.

Output:
{"facts": [
  {"key":"user_espresso_machine_purchase",
   "value":"User bought a Breville espresso machine for $400 on 2024-05-11 (yesterday).",
   "tags":["purchase","date"],"entities":["breville espresso machine"],
   "relations":[{"s":"user","v":"bought","o":"breville espresso machine"}],
   "temporal_anchor":"2024-05-11",
   "derived":[{"predicate":"acquire","object":"breville espresso machine",
               "category":"kitchen appliance","numeric_value":400,
               "unit":"USD","occurred_at":"2024-05-11"}],
   "source_message_indices":[0]}
]}

EXAMPLE 20 — derived occurrence record: a visit/appointment instance.
Each visit is one countable occurrence with a date. This applies to
PLACE visits too ("went to / stopped by / visited the market, gym,
museum, restaurant"): emit an "attend" occurrence even when no amount
is stated — "how many times did I go to X" needs each visit counted.

observation_date: 2023-09-02

Input messages:
  [user] Saw my physiotherapist this morning about my knee, then stopped by the Riverside farmers market on the way home.

Output:
{"facts": [
  {"key":"user_physiotherapist_visit",
   "value":"User saw their physiotherapist on 2023-09-02 (this morning) about their knee.",
   "tags":["health","date"],"entities":["physiotherapist","knee"],
   "relations":[{"s":"user","v":"consulted","o":"physiotherapist"}],
   "temporal_anchor":"2023-09-02",
   "derived":[{"predicate":"consult","object":"physiotherapist",
               "category":"appointment","numeric_value":null,
               "unit":"count","occurred_at":"2023-09-02"}],
   "source_message_indices":[0]},
  {"key":"user_farmers_market_visit",
   "value":"User stopped by the Riverside farmers market on 2023-09-02.",
   "tags":["activity","date"],"entities":["riverside farmers market"],
   "relations":[{"s":"user","v":"visited","o":"riverside farmers market"}],
   "temporal_anchor":"2023-09-02",
   "derived":[{"predicate":"attend","object":"riverside farmers market",
               "category":"farmers market","numeric_value":null,
               "unit":"count","occurred_at":"2023-09-02"}],
   "source_message_indices":[0]}
]}

EXAMPLE 21 — started/finished transitions are dated occurrences too.
Each transition gets its own fact, its own anchor, and its own derived
record — these are the operands for "how long did it take" questions.

observation_date: 2025-02-20

Input messages:
  [user] I finally finished the sweater I started knitting in early January.

Output:
{"facts": [
  {"key":"user_sweater_finished",
   "value":"User finished knitting the sweater on 2025-02-20.",
   "tags":["hobby","milestone","date"],"entities":["sweater"],
   "temporal_anchor":"2025-02-20",
   "derived":[{"predicate":"finish","object":"sweater",
               "category":"knitting project","numeric_value":null,
               "unit":"count","occurred_at":"2025-02-20"}],
   "source_message_indices":[0]},
  {"key":"user_sweater_started",
   "value":"User started knitting the sweater in early January 2025 (~2025-01-05).",
   "tags":["hobby","date"],"entities":["sweater"],
   "temporal_anchor":"2025-01-05",
   "derived":[{"predicate":"start","object":"sweater",
               "category":"knitting project","numeric_value":null,
               "unit":"count","occurred_at":"2025-01-05"}],
   "source_message_indices":[0]}
]}

EXAMPLE 22 — stated totals are NOT occurrences. "I now have five X"
states a CURRENT TOTAL (a value that supersedes earlier totals), not a
new countable event — emit NO derived record. A derived count row here
would double-count against the stated total. Contrast: "I picked up
two more plants today" IS an occurrence (derived numeric_value 2).

observation_date: 2024-06-02

Input messages:
  [user] I now have five houseplants.

Output:
{"facts": [
  {"key":"user_houseplant_count",
   "value":"User now has five houseplants (as of 2024-06-02).",
   "tags":["possession"],"entities":["houseplants"],
   "source_message_indices":[0]}
]}

EXAMPLE 23 — money RAISED / EARNED / DONATED / GIVEN / CONTRIBUTED at an
event is a SUMMABLE occurrence with a USD amount, exactly like a purchase.
The $ amount MUST become numeric_value (a charity row with numeric_value
null cannot be summed). Emit the derived record with the amount and a
STABLE category — when the cause is a charity / fundraiser / good cause,
use category "charity" so amounts across SEPARATE events with different
beneficiaries GROUP and sum under one heading. The amount is the operand
for "how much did I raise / donate / give to charity in total" questions
and MUST be materialized, not left only in prose. (A later RE-MENTION of
the SAME event — "I raised $450 at that bake sale, like I said" — is the
same occurrence: do NOT emit a second derived row for it, or the SUM
double-counts.)

observation_date: 2024-03-08

Input messages:
  [user] I helped run a bake sale last Saturday and we raised $450 for
         the children's hospital.

Output:
{"facts": [
  {"key":"user_bake_sale_fundraiser",
   "value":"User helped run a bake sale on 2024-03-02 (last Saturday) and raised $450 for the children's hospital.",
   "tags":["volunteering","fundraising","date"],
   "entities":["bake sale","children's hospital"],
   "relations":[{"s":"user","v":"raised","o":"$450"}],
   "temporal_anchor":"2024-03-02",
   "derived":[{"predicate":"raise","object":"bake sale",
               "category":"charity","numeric_value":450,
               "unit":"USD","occurred_at":"2024-03-02"}],
   "source_message_indices":[0]}
]}

EXAMPLE 24 — a social occasion ATTENDED (a party, dinner, gathering,
celebration) is a countable "attend" occurrence — one per occasion,
each with its own date — the same way a place visit is (Example 20).
"How many parties / gatherings / events did I attend" counts each
distinct occasion. Emit the "attend" record even when no amount is
stated. A PLANNED or merely-considered occasion ("I'm hosting a party
soon", "thinking of a game night") is NOT an occurrence — no derived
record until it actually happened.

observation_date: 2023-11-20

Input messages:
  [user] Went to Mark's housewarming party on Friday — great turnout.

Output:
{"facts": [
  {"key":"user_mark_housewarming_party",
   "value":"User attended Mark's housewarming party on 2023-11-17 (Friday).",
   "tags":["social","date"],"entities":["mark","housewarming party"],
   "relations":[{"s":"user","v":"attended","o":"housewarming party"}],
   "temporal_anchor":"2023-11-17",
   "derived":[{"predicate":"attend","object":"mark's housewarming party",
               "category":"party","numeric_value":null,
               "unit":"count","occurred_at":"2023-11-17"}],
   "source_message_indices":[0]}
]}

EXAMPLE 25 — dinner gatherings (a dinner party, a potluck, a feast at
someone's place) the user ATTENDED are countable "attend" occurrences,
one per occasion, each dated. Use the STABLE category "dinner party" so
separate dinners GROUP for "how many dinner parties did I attend" counts.
A dinner the user is HOSTING or planning ("a dinner party I'm hosting
soon") is NOT a record — only occasions that already happened count.
Resolve relative dates against observation_date.

observation_date: 2024-02-18

Input messages:
  [user] I'm looking for recipe ideas for a dinner party I'm hosting soon.
         I went to a lovely feast at Priya's place last week, and a few
         days before that I was at a potluck at Dan's.

Output:
{"facts": [
  {"key":"user_priya_dinner",
   "value":"User attended a dinner feast at Priya's place on 2024-02-11 (last week).",
   "tags":["social","date"],"entities":["priya"],
   "relations":[{"s":"user","v":"attended","o":"priya's dinner"}],
   "temporal_anchor":"2024-02-11",
   "derived":[{"predicate":"attend","object":"priya's dinner feast",
               "category":"dinner party","numeric_value":null,
               "unit":"count","occurred_at":"2024-02-11"}],
   "source_message_indices":[0]},
  {"key":"user_dan_potluck",
   "value":"User attended a potluck at Dan's place around 2024-02-08.",
   "tags":["social","date"],"entities":["dan","potluck"],
   "relations":[{"s":"user","v":"attended","o":"dan's potluck"}],
   "temporal_anchor":"2024-02-08",
   "derived":[{"predicate":"attend","object":"dan's potluck",
               "category":"dinner party","numeric_value":null,
               "unit":"count","occurred_at":"2024-02-08"}],
   "source_message_indices":[0]}
]}

The dinner party the user is HOSTING gets NO derived record (it hasn't
happened — see Example 24's planned-occasion rule). The two occasions
the user attended each get one "attend" record under the shared
"dinner party" category, so the cross-session COUNT is exactly two.

EXAMPLE 26 — STATE CHANGE: a COUNT changed across turns. The new count
is its OWN dated fact; do NOT average ("10" then "15" is NOT "12"), do
NOT sum into a total, do NOT drop either value. existing_memories holds
the prior count; the prior session said 10, this session says 15.

observation_date: 2023-09-30

existing_memories:
  - {key:"user_crash_course_progress", value:"User has watched 10 Crash Course videos (as of 2023-08-11)."}

Input messages:
  [user] Quick update — I'm now up to 15 Crash Course videos watched.

Output:
{"facts": [
  {"key":"user_crash_course_progress",
   "value":"User has now watched 15 Crash Course videos (as of 2023-09-30).",
   "tags":["learning","progress","date"],"entities":["crash course"],
   "linked_keys":[0],
   "temporal_anchor":"2023-09-30",
   "source_message_indices":[0]}
]}

The new value (15) is emitted verbatim as its own fact, dated to this
session, linked to the prior. The prior "10" fact already exists and is
NOT re-emitted or rewritten. NO invented "12". A later "how many videos
has the user watched now?" reads the latest-dated value → 15.

EXAMPLE 27 — STATE CHANGE: a STATUS / frequency changed. The new value
is its own clean dated fact naming the SAME attribute as the prior, so
the latest is unambiguous. Do not let the new value survive only as a
vague aside.

observation_date: 2024-02-01

existing_memories:
  - {key:"user_therapy_frequency", value:"User sees their therapist every two weeks (as of 2023-11-03)."}

Input messages:
  [user] I bumped up therapy — I'm seeing my therapist every week now.

Output:
{"facts": [
  {"key":"user_therapy_frequency",
   "value":"User now sees their therapist every week (as of 2024-02-01).",
   "tags":["health","schedule","change","date"],"entities":["therapist"],
   "linked_keys":[0],
   "temporal_anchor":"2024-02-01",
   "source_message_indices":[0]}
]}

The latest frequency ("every week") is a first-class dated fact, not
blended with the prior "every two weeks" and not dropped behind it — and it
reuses the SAME key as the prior: the store keeps both versions under one
key and reads the newest; a `_update` / `_new` suffix would split one
attribute across two keys and leave the old value looking current.

EXAMPLE 28 — STATE CHANGE: a LOCATION changed (then changed again). Each
stated location is its OWN dated fact; the most recent one wins by date.
Never merge "Chicago" and "the suburbs" into one, never drop the latest.

observation_date: 2023-05-27

existing_memories:
  - {key:"user_friend_rachel_move", value:"Rachel moved to a new apartment in Chicago (as of 2023-05-24)."}

Input messages:
  [user] Update on Rachel — she actually moved back to the suburbs.

Output:
{"facts": [
  {"key":"user_friend_rachel_move",
   "value":"Rachel moved back to the suburbs (as of 2023-05-27).",
   "tags":["relationship","location","change","date"],"entities":["rachel","suburbs"],
   "relations":[{"s":"rachel","v":"moved_to","o":"suburbs"}],
   "linked_keys":[0],
   "temporal_anchor":"2023-05-27",
   "source_message_indices":[0]}
]}

The new location ("the suburbs") is captured verbatim as its own dated
fact. The prior "Chicago" fact stays. "Where does Rachel live now?"
resolves to the latest-dated value → the suburbs.

EXAMPLE 29 — EVENT date vs BOOKING/mention date. The flight HAPPENS on
March 3; that is the event date and the anchor. The day the user is
talking about it (observation_date) is the booking/mention date, NOT the
anchor. Do not stamp the conversation date onto a future or past event.

observation_date: 2024-02-20

Input messages:
  [user] I just booked a flight to Denver for March 3.

Output:
{"facts": [
  {"key":"user_denver_flight",
   "value":"User booked a flight to Denver for 2024-03-03.",
   "tags":["travel","plan","date"],"entities":["denver"],
   "temporal_anchor":"2024-03-03",
   "source_message_indices":[0]}
]}

The anchor is the flight date (Mar 3), not the booking day (Feb 20). A
"how long until / when is the trip?" question needs the event date.

EXAMPLE 30 — AMBIENT OBSERVATION with a relative qualifier. Reads like
timeless prose but pins to a real date the answer needs. Resolve the
qualifier against observation_date, anchor it, and keep the raw phrase
in the value.

observation_date: 2023-05-20

Input messages:
  [user] The American goldfinches came back to the feeder about a week ago.

Output:
{"facts": [
  {"key":"user_goldfinches_return",
   "value":"User noticed the American goldfinches returned to the feeder around 2023-05-13 (about a week ago).",
   "tags":["nature","observation","date"],"entities":["american goldfinches"],
   "temporal_anchor":"2023-05-13",
   "source_message_indices":[0]}
]}

Even though "goldfinches returning" sounds ambient/timeless, it pins to
~May 13. Without the anchor, "how long between getting the binoculars
and the goldfinches returning?" cannot be computed.

EXAMPLE 31 — NAMED MONTH with no day; resolve to the most recent
matching month ON OR BEFORE observation_date (month precision → first of
month). Do NOT drift the year — "in January" with a May-2024 session is
January 2024, not 2023 and not 2025.

observation_date: 2024-05-10

Input messages:
  [user] My sister had her first baby back in January.

Output:
{"facts": [
  {"key":"user_sister_first_child",
   "value":"User's sister had her first baby in January 2024 (~2024-01-01).",
   "tags":["family","milestone","date"],"entities":["sister"],
   "temporal_anchor":"2024-01-01",
   "source_message_indices":[0]}
]}

"in January" resolves to the most recent January on or before the
session (Jan 2024). A "whose milestone came first?" ordering question
needs this anchored, not left as a bare month name.

EXAMPLE 32 — ELAPSED-DURATION operand. The user reports they STARTED /
BEGAN / PICKED UP an ONGOING activity (not yet finished) on or at a
resolvable date, OR states an explicit DURATION ("for three weeks",
"for the past two months") for it. Capture the START as a durable,
ANCHORED fact — the start date IS the operand a later "how long had I
been doing X when Y happened?" question subtracts from. A start-anchor
fact answers the duration even though the user never said a number. When
the user ALSO states an explicit duration, keep that figure verbatim in
the value too (a second operand). This is the ONGOING-activity sibling
of Example 21's discrete finished project: there the question is "how
long did the finished thing take"; here the activity is still going and
the question times it against a later event.

observation_date: 2024-06-10

Input messages:
  [user] I started watching stand-up comedy specials regularly back in
         March — been hooked ever since.

Output:
{"facts": [
  {"key":"user_comedy_specials_start",
   "value":"User started watching stand-up comedy specials regularly in March 2024 (~2024-03-01).",
   "tags":["hobby","date"],"entities":["stand-up comedy specials"],
   "relations":[{"s":"user","v":"started_watching","o":"stand-up comedy specials"}],
   "temporal_anchor":"2024-03-01",
   "derived":[{"predicate":"start","object":"stand-up comedy specials",
               "category":"hobby","numeric_value":null,
               "unit":"count","occurred_at":"2024-03-01"}],
   "source_message_indices":[0]}
]}

"in March" resolves to the most recent March on or before the session
(~2024-03-01). The activity is ongoing ("ever since"), so this is a
START anchor, not a finished transition — no second "finish" fact. A
later "how long had I been watching comedy specials when I attended the
festival on May 20?" subtracts the start anchor from the festival date.
Without the anchored start, that span cannot be computed.

EXAMPLE 32b — EXPLICIT stated duration. The user gives a duration figure
("for N weeks/months/years") for how long they have been doing or had
done something. Keep the figure VERBATIM in the value AND materialize it
as a derived duration operand (numeric_value + unit). Do NOT convert
units (keep "weeks" if the user said weeks) and do NOT compute an end
date the user did not state.

observation_date: 2023-10-15

Input messages:
  [user] I'd been using my binoculars for about six weeks before the
         lens fogged up.

Output:
{"facts": [
  {"key":"user_binoculars_usage_duration",
   "value":"User had been using their binoculars for about six weeks before the lens fogged up.",
   "tags":["possession","duration"],"entities":["binoculars"],
   "relations":[{"s":"user","v":"used","o":"binoculars"}],
   "temporal_anchor":"2023-10-15",
   "derived":[{"predicate":"use","object":"binoculars",
               "category":"equipment","numeric_value":6,
               "unit":"weeks","occurred_at":"2023-10-15"}],
   "source_message_indices":[0]}
]}

"about six weeks" is the explicit duration operand — 6 / "weeks" become
numeric_value + unit so "how long did I use my binoculars before the
lens fogged?" reads the materialized number. The hedge ("about") stays
verbatim in the value; no end date is invented beyond the stated event.

EXAMPLE 33 — PRECISION GUARD for durations: do NOT manufacture a start
date or a duration the user never stated. When the user mentions an
ongoing activity with NO start, NO "since", NO "for N weeks", and NO
resolvable date qualifier, emit the fact as a TIMELESS standing state —
temporal_anchor null, NO derived duration record, NO invented number.
Over-stamping a fabricated start would let the reader compute a span the
user never supported.

observation_date: 2024-06-10

Input messages:
  [user] I watch a lot of stand-up comedy specials — they're my go-to
         after a long day.

Output:
{"facts": [
  {"key":"user_comedy_specials_preference",
   "value":"User watches a lot of stand-up comedy specials as their go-to after a long day.",
   "tags":["preference","hobby","preference:entertainment"],
   "entities":["stand-up comedy specials"],
   "source_message_indices":[0]}
]}

No "started", no "since March", no "for N weeks" — just a standing
habit. It is a durable preference (temporal_anchor null), NOT a dated
start. Do NOT invent a start date or a duration; manufacturing one would
answer "how long have I been watching?" with a number the user never
gave.

EXAMPLE 34 — FORESIGHT: a bounded, forward-looking or temporary state gets
a `foresight` window so it stops applying once the window has passed. The
plan's date and the condition's length are the window; the fact itself is
stated plainly. A durable preference in the same turn carries NO foresight.

observation_date: 2025-10-06

Input messages:
  [user] The dentist put me on antibiotics for two weeks after yesterday's
         extraction, so no beer for now. I fly to Lisbon on the 20th for a
         week — still an IPA person, though.

Output:
{"facts": [
  {"key":"user_antibiotics_course",
   "value":"User is on a two-week course of antibiotics prescribed by the dentist after a tooth extraction on 2025-10-05.",
   "tags":["health","medication"],
   "entities":["dentist","antibiotics"],
   "temporal_anchor":"2025-10-05","temporal_confidence":0.9,
   "valid_at":"2025-10-05",
   "foresight":{"until":null,"duration_days":14},
   "source_message_indices":[0]},
  {"key":"user_lisbon_trip",
   "value":"User flies to Lisbon on 2025-10-20 for a week.",
   "tags":["travel","plan"],
   "entities":["lisbon"],
   "temporal_anchor":"2025-10-20","temporal_confidence":0.9,
   "foresight":{"until":"2025-10-27","duration_days":7},
   "source_message_indices":[0]},
  {"key":"user_beer_preference",
   "value":"User's preferred beer style is IPA.",
   "tags":["preference","preference:beer","drink"],
   "entities":["ipa"],
   "source_message_indices":[0]}
]}

The antibiotics course ends 14 days after it started; the trip ends a
week after the 20th; the IPA preference is durable and carries nothing.
A reader asked "recommend a drink" on 2025-10-12 should see the course
and not the beer; asked on 2025-11-01 it should see neither.

EXAMPLE 35 — OWNERSHIP and a REQUEST TO FORGET. A fact about another
person carries `about: "other"` (the key names that person); a request not
to remember something is a `forget` entry that names the existing memory
to retire — it is not itself a memory, and the forgotten information is
not re-emitted as a fact.

observation_date: 2025-11-02

existing_memories:
  [0] {key:"user_salary", value:"User earns $120k a year."}
  [1] {key:"user_coffee_preference", value:"User prefers dark roast coffee."}

Input messages:
  [user] Can you tidy this note to my sister? "Hey Priya — I'll bring the
         decaf, I know you can't do caffeine after noon. See you Sunday."
         Also, please forget what I told you about my salary earlier.

Output:
{"facts": [
  {"key":"user_sister_priya_caffeine",
   "value":"User's sister Priya does not drink caffeinated coffee after noon and takes decaf.",
   "tags":["family","preference:drink"],
   "entities":["priya"],
   "about":"other",
   "relations":[{"s":"user","v":"sister","o":"priya"}],
   "source_message_indices":[0]},
  {"key":"user_salary",
   "value":"User asked the assistant to forget their salary.",
   "tags":["privacy","forget"],
   "forget":true,
   "linked_keys":[0],
   "source_message_indices":[0]}
]}

Priya's coffee rule is HER preference, so it is `about: "other"` — a
reader recommending coffee for the USER must not switch them to decaf. The
salary request retires memory [0]; the $120k figure is not repeated
anywhere in the output. The user's own dark-roast preference (memory [1])
is untouched.

EXAMPLE 36 — IMPLICIT PERSONA: the user asks for an edit and the text
they hand over reveals durable facts about them. The facts are extracted;
the editing task is not.

observation_date: 2025-09-03

Input messages:
  [user] Can you make this email to my manager sound a bit more confident?
         "Hi Dana — I'll be offline Thursday mornings for my physio
         appointments through October (the knee is finally improving).
         I've moved the stand-up to 9:30 on those days. I've also started
         cycling in, so I may arrive a few minutes after 9 on other days."
  [assistant] Here's a more confident version: …

Output:
{"facts": [
  {"key":"user_physio_appointments",
   "value":"User has physiotherapy appointments on Thursday mornings through October 2025 for a knee that is improving.",
   "tags":["health","schedule"],
   "entities":["physiotherapy"],
   "temporal_anchor":"2025-09-03","temporal_confidence":0.7,
   "foresight":{"until":"2025-10-31","duration_days":null},
   "source_message_indices":[0]},
  {"key":"user_commute_mode",
   "value":"User has started cycling to work.",
   "tags":["preference","preference:commute","lifestyle"],
   "entities":["cycling"],
   "valid_at":"2025-09-03",
   "source_message_indices":[0]},
  {"key":"user_manager",
   "value":"User's manager is Dana.",
   "tags":["work","relationship"],
   "entities":["dana"],
   "relations":[{"s":"user","v":"reports_to","o":"dana"}],
   "source_message_indices":[0]}
]}

Nothing here says "user asked to make an email more confident": that is
the task, not a fact. The physio schedule is bounded (foresight through
October), the commute change is a durable preference with the Preference
rule applied, and the manager's name is a relationship fact.

EXAMPLE 37 — ASSISTANT PREMISE ABOUT THE USER: the assistant grounds its
answer in something it knows about the user, and the user lets it stand.
The premise is the user's fact; the recommendations stay the assistant's.

observation_date: 2025-09-03

Input messages:
  [user] Can you suggest some engaging audio series that can keep me
         entertained during my daily commute, ideally something I can
         also learn from?
  [assistant] Since you enjoy podcasts about world history, you might like
         'Hardcore History' for deep dives into pivotal events, or
         'Throughline' for how the past shapes current events. Both are
         easy to follow on a commute.
  [user] Great, I'll try Throughline tomorrow.

Output:
{"facts": [
  {"key":"user_podcast_taste",
   "value":"User enjoys podcasts about world history.",
   "tags":["preference","preference:podcast","interest"],
   "entities":["world history"],
   "topics":["history podcasts"],
   "source_message_indices":[1]},
  {"key":"assistant_recommended_hardcore_history",
   "value":"Assistant recommended the podcast Hardcore History for deep dives into pivotal historical events.",
   "tags":["recommendation","podcast"],
   "entities":["hardcore history"],
   "relations":[{"s":"assistant","v":"recommended","o":"hardcore history"}],
   "source_message_indices":[1]},
  {"key":"assistant_recommended_throughline",
   "value":"Assistant recommended the podcast Throughline for how the past shapes current events; the user plans to try it.",
   "tags":["recommendation","podcast"],
   "entities":["throughline"],
   "relations":[{"s":"assistant","v":"recommended","o":"throughline"}],
   "source_message_indices":[1,2]}
]}

"Since you enjoy podcasts about world history" is stated by the assistant
about the user and not corrected: it is the user's preference, keyed to the
user, in the user's voice, with the Preference rule applied — not a line
inside the recommendation. Had the assistant written "If you enjoy history
podcasts…", nothing about the user would be extracted.
"""


# Appended to the extraction system prompt only when
# `memory_capture_structured_answers` is on. Anchors the LONG / ENUMERATED
# / POSITIONAL assistant-answer shape (Examples 8f/8g) so it is captured
# whole and in order rather than dropped or lossily summarised. The 8g
# precision guard keeps non-committal option-listing out of the pool.
_STRUCTURED_ANSWER_PROMPT_BLOCK = """\
==============================================================
STRUCTURED / ENUMERATED ASSISTANT ANSWERS — CAPTURE WHOLE
==============================================================

When the assistant answers the user's FACTUAL question with a
STRUCTURED, ENUMERATED, ORDERED, or LONG answer — a list, an ordered
sequence, a multi-step procedure, a multi-item enumeration, OR a
specific item identified BY ITS POSITION in a longer list ("the 27th
one is X") — capture it as ONE durable fact that PRESERVES THE FULL
STRUCTURE VERBATIM:

  • Keep the ENTIRE sequence / list / set of steps, IN ORDER, in a
    SINGLE fact's `value`. Do NOT truncate it. Do NOT summarise it
    ("a long chord progression", "several airlines"). Do NOT fragment
    it into one fact per element (that destroys the order and the
    positions a later question needs).
  • When the answer pins a value to a POSITION ("the 27th parameter
    was 'Sound'", "the third step is X"), keep BOTH the position AND
    the value verbatim — the later question is "what was the Nth one?".
  • Subject = the assistant. Set `question_topic` to the SUBJECT the
    user's question was about (phrased from the question, not the
    answer content — see Examples 8c/8e), so a low-overlap follow-up
    that names only the topic can find the intact answer later.
  • This OVERRIDES the usual atomic-fact discipline FOR THIS ANSWER
    ONLY: the structured answer is intentionally one large fact. Every
    OTHER fact in the same transcript stays atomic as usual.

PRECISION GUARD — only a COMMITTED answer counts. A genuine answer is
one the assistant gives AS the answer to the user's factual question:
the chord progression they recommend, the ranked list they provide,
the item at the requested position. It is NOT:
  • an open menu of OPTIONS the assistant floats with no commitment
    ("here are some options you might consider…", "you could try any
    of these") — that is a non-answer enumeration; capture nothing,
  • assistant chatter, hedges, caveats, or "it depends" framing,
  • a list the assistant gives as background while DEFERRING the actual
    choice back to the user.
When the assistant lists candidates without committing to an answer,
emit NO structured-answer fact. Over-capturing non-answers dilutes
retrieval — the exact failure this block must avoid.

EXAMPLE 8f — LONG / ENUMERATED / POSITIONAL assistant answer captured
WHOLE. The user asks a factual question; the assistant answers with a
long ordered list and, later in the same turn, pins one item by its
POSITION. Both the full list (in order) and the position→value pinning
survive in ONE fact's value. A later low-overlap follow-up ("what did
you say the 27th setting was?", "remind me that list of settings") cites
only the TOPIC, never the item content, so question_topic is what makes
the intact answer findable.

Input messages:
  [user]      Can you list the synth's parameters in order? I want to
              know what the 27th one controls.
  [assistant] Sure — in order they are: 1. Volume, 2. Pan, 3. Tune,
              4. Fine, 5. Glide, 6. Attack, 7. Decay, 8. Sustain,
              9. Release, ... 26. Reverb, 27. Sound, 28. Mix. So the
              27th parameter is "Sound".
  [user]      got it, thanks

Output:
{"facts": [
  {"key":"assistant_listed_synth_parameters",
   "value":"Assistant listed the synth parameters in order (1. Volume, 2. Pan, 3. Tune, 4. Fine, 5. Glide, 6. Attack, 7. Decay, 8. Sustain, 9. Release, ... 26. Reverb, 27. Sound, 28. Mix) and said the 27th parameter is \\"Sound\\".",
   "tags":["answer","reference"],"entities":["sound"],
   "relations":[{"s":"assistant","v":"listed","o":"synth parameters"}],
   "source_message_indices":[1],
   "question_topic":"the synth parameter list and the 27th parameter"}
]}

The whole ordered list AND the "27th = Sound" pinning stay in ONE value
— never split per parameter, never summarised to "the parameter list".
question_topic is phrased from the question (the parameter list / the
27th one), not the answer ("Sound"), because the follow-up overlaps the
topic, not the value. "got it, thanks" is filler and produces no fact.

EXAMPLE 8g — PRECISION GUARD: the assistant floats an OPEN MENU of
options with NO commitment. This is a non-answer enumeration — capture
NOTHING from it. Contrast with 8b (the assistant NAMES specific apps as
its recommendation → those ARE facts): here the assistant explicitly
defers the choice back to the user, so there is no answer to record.

Input messages:
  [user]      What font should I use for the slide deck?
  [assistant] There are lots of good choices — you might consider
              Helvetica, Georgia, or Futura, among many others. It
              really depends on the vibe you're going for; up to you!
  [user]      ok I'll think about it

Output:
{"facts": []}

The assistant committed to nothing ("you might consider", "it depends",
"up to you") — this is an open menu, not an answer. Emitting
"assistant suggested fonts" would manufacture a distractor fact and
dilute retrieval. Capture NOTHING. (Had the assistant said "Go with
Helvetica" — a commitment — that WOULD be an answer fact with
question_topic "the font for the slide deck".)
"""


_JUDGE_SYSTEM_PROMPT = """You judge extracted memorable facts against
the source messages they were extracted from.

Inputs:
- source messages (list)
- extracted facts: each has key, value, supporting message

Task: for each fact, return a verdict:

{"verdicts": [
  {"key": "user_dog_name", "verdict": "CORRECT"},
  {"key": "user_employer", "verdict": "WRONG",
   "reason": "messages mention Bob's employer, not user's"},
  {"key": "deploy_schedule", "verdict": "UNCERTAIN",
   "reason": "messages say 'usually Tuesdays' — not committed"},
  ...
]}

Verdict rules:
- CORRECT: the source messages explicitly support the fact's value.
- WRONG: the source contradicts the value, or the value contains
  detail the source never states.
- UNCERTAIN: the source is silent, ambiguous, off-topic, or hedged
  on this fact — neither supports nor refutes. Default to UNCERTAIN
  whenever you would have to infer or fill in to call it CORRECT.

TEMPORAL RESOLUTION EXCEPTION: when the extractor is given an
`observation_date`, it resolves relative time expressions ("last week",
"next March", "three weeks ago") to absolute dates. These resolutions
are CORRECT even though the literal date doesn't appear in the source
messages — judge them by whether the math is plausible against the
observation_date provided below. "next March" with observation_date in
May 2026 → "March 2027" is correct. Do NOT mark resolved dates as
fabricated.

ASSISTANT-STATED-FACT EXCEPTION: when a fact records something the
ASSISTANT said, recommended, answered, or listed (a name, value, number,
date, quote, or recommendation), the assistant's own utterance IS the
explicit support — grade it CORRECT even if the user never confirmed it
and even if the assistant phrased it briefly or in passing. Users later
ask "remind me what you said / what you recommended", so an answer the
assistant gave is a real fact. Do NOT mark an assistant-supplied fact
UNCERTAIN or WRONG merely because the user didn't restate it; only use
WRONG if the source actually CONTRADICTS the recorded value.

Output ONLY the JSON object. No commentary."""


# Appended to the system prompt on the retry triggered when non-trivial
# input yielded zero facts (a likely extraction misfire). Pushes the
# model to re-examine before accepting an empty result.
# The provider's content filter cut the previous reply, as it does when a reply
# reproduces a long published passage word for word. The facts are still wanted;
# the passage is not.
_NO_RECITATION_ADDENDUM = """

The previous reply was cut off by the provider's content filter. Do not
reproduce long passages, quotes, lyrics or text from published works; state
what a passage is (its source, its point) in your own words, one short sentence.
Return every fact as before.
"""

_RETRY_PROMPT_ADDENDUM = """

CRITICAL: On the previous attempt you returned zero facts despite the
input containing substantive content. Re-examine. A "memorable fact"
includes stated preferences, named entities, dates, decisions,
relationships, and any concrete value the user mentioned. If after
careful re-examination there is genuinely nothing memorable, return
{"facts": []} — but only after looking carefully.
"""


# A SMALL input (one decision, one preference — the shape an
# agent's tool call sends) does not need the 49 schema anchors written for
# long conversations (topic chains, cross-turn state changes, dinner parties,
# multi-date timelines). The compact prompt keeps every rule and the output
# schema byte-for-byte, and only the examples whose input shape a short
# statement can take: a decision, a correction, a dated purchase, a started
# activity. The set is measured, not picked: adding 3e, 3f, 32b, 33 or 20
# made the forget-and-switch case worse. The full prompt is 21.6K tokens for
# a one-line input; the compact one 13.0K.
_COMPACT_EXAMPLE_IDS: frozenset[str] = frozenset(
    {
        "1",
        "2",
        "3",
        "3b",
        "3c",
        "3d",
        "4",
        "5",
        "6",
        "7",
        "8",
        "8c",
        "8d",
        "9",
        "9b",
        "10",
        "16",
        "19",
        "21",
        "22",
        "32",
        "34",
        "35",
    }
)
_EXAMPLES_HEADER = "EXAMPLES (schema anchors"


def compact_extraction_prompt(prompt: str, keep: frozenset[str] = _COMPACT_EXAMPLE_IDS) -> str:
    """`prompt` with only the EXAMPLE blocks whose id is in `keep`. The rules
    before the examples header are returned unchanged; a prompt with no
    examples header is returned as is."""
    import re

    at = prompt.find(_EXAMPLES_HEADER)
    if at < 0:
        return prompt
    body_start = prompt.find("\nEXAMPLE ", at)
    if body_start < 0:
        return prompt
    head, body = prompt[:body_start], prompt[body_start:]
    blocks = re.split(r"\n(?=EXAMPLE [0-9]+[a-z]? — )", body)
    kept = [
        b for b in blocks if (m := re.match(r"EXAMPLE ([0-9]+[a-z]?) — ", b)) and m.group(1) in keep
    ]
    return head + "\n" + "\n".join(kept) + ("\n" if kept else "")


# The durability verifier over a coding capture's remaining
# facts (memory/app/durability_verifier.py). Part A is the judge's rubric: the
# LLM decides real vs junk. A softer wording ("measured results", "when
# unsure, KEEP") dropped only a small share of what the judge calls junk.
# Part B is the same-claim folds.
DURABILITY_VERIFIER_PROMPT_VERSION = "durability-verifier-v1"

DURABILITY_VERIFIER_SYSTEM_PROMPT = """You review facts that an extractor pulled from one AI coding session (Claude Code, Codex) before they are stored in a long-term memory that future sessions search.

Do two things.

A. For each fact, decide KEEP or DROP.
KEEP a fact if it would still be true and useful to recall in a week: a fact about the user, their preferences or decisions, the project, the codebase, or the world.
DROP a fact if it narrates this session's own activity, restates a tool's output or a file's text, records a transient state (a status, a count at a moment, a version just written), is test chatter, or is a vague/garbled fragment.

When the session goal and its lookups are shown: a session that asked Loom or a tool to look something up (is this already reported, which PR caused this, search the issues) mostly produces facts that repeat what the lookup returned: an issue's title, state or fix, a PR's number, merge date, versions or author, a workaround quoted from the record. DROP those; the tracker and the code index already hold them. The same holds for a fact whose subject is the assistant, the agent or the user reporting a finding ("Assistant stated…", "Assistant identified…", "User was told…", "The check found…"). KEEP only what the lookup could not have supplied: what the user decided, prefers or said about their own setup, deployment or plans.

B. List groups of facts that state the SAME claim (the same subject and the same assertion, even in other words; a fact that only adds detail to another is the same claim). Facts that are merely about the same topic are NOT a group. For each group give the number of the most complete member. Also list pairs where a later fact replaces an earlier one (a preference that changed, a bug a later fact says is fixed, a policy that was replaced).

Answer JSON only:
{"verdicts": [{"i": <n>, "keep": true|false, "why": "<5-12 words>"}],
 "same_claim": [{"members": [<n>, ...], "best": <n>}],
 "superseded": [{"old": <n>, "new": <n>}]}"""
