"""The ruleset: which help topics a message raises, which outcomes the backend mandates, and the routine draft.

Pure functions over dicts: nothing here touches the database or a provider, and nothing else from the app is
imported. The three tables to audit are TOPICS (the phrases of each help topic), SENTENCES (everything a draft can
say) and DRAFTS (which sentences answer a topic for an item in each state).
"""
import re
from datetime import date

INTENTS = ("track", "refund", "cancel", "return", "missing")  # the quick actions of the chat page
QUICK = {"track": "track_status", "refund": "refund_status", "cancel": "cancel", "return": "return",
         "missing": "not_received"}  # the topic each quick action stands for
ACTIONS = ("cancel", "return")  # the two things a customer can confirm
MAX_TOPICS = 3  # parts of one message that a draft answers
MAX_WORDS = 80  # a draft that combines several parts is cut back to this length

CHOOSE_ITEM = "Which item is this about? Please choose one."
UNKNOWN_ITEM = "I could not find that item in your orders. Which item is this about?"
TICKET_FAILED = "I could not open a support ticket just now. Please try again."
LIMIT_REACHED = "This demo session has reached its message limit. Reload the page to start again."
NOT_UNDERSTOOD = (
    "Sorry, I did not understand that. I can help with your orders (status, delivery, cancellation, returns,"
    " exchanges, refunds, payment, invoice, address and order changes) and with our store policy, or pass you"
    " to a person."
)

# ---- Detection ------------------------------------------------------------------------------------------------
# ponytail: phrase tables, not language understanding. A topic is found when one of its phrases is in the
# message; wording that no phrase covers finds nothing (the fallback or a person answers), and a phrase meant
# otherwise finds a topic the customer did not raise (the draft then says more than was asked, or a backend rule
# opens a ticket that hands the customer to a person). Upgrade path: an intent classifier trained on real chats.
#
# How a phrase is read, after _plain() has lower-cased the message and spelt contractions out ("haven't" and
# "havent" become "have not", "where's" becomes "where is"):
#   words match whole words;   a|b  either word;   cancel*  the word may go on (cancelled, cancellation);
#   ..  up to six other words may stand in between.
_NOT = ("not", "not yet", "not been", "not yet been", "not even", "never", "never been", "yet to", "yet to be")
_COME = ("receiv*", "got", "get", "gotten", "arriv*", "come", "came", "reach*", "deliver*", "here", "turn* up",
         "show* up")
_POLICY_ON = "polic* on|for|about|regarding|of|around"

TOPICS: dict[str, tuple[str, ...]] = {
    # -- order topics: about one item --
    "track_status": (
        "where is|are my|the|it|this|that|they|our", "where has|have|did|could", "where it|my|the|this|they",
        "whereabouts", "status",
        "any update|updates|news", "update|updates on|about|regarding", "what is happening", "what happened to|with",
        "what is going on", "has|have|is|are|was|were it|my|the|this|that|they .. shipped|dispatched|delivered|packed",
        "did it|my|the|this|that|they .. ship|dispatch|leave|arrive|come|reach",
        "been shipped|dispatched|delivered|packed", "shipped|dispatched|delivered|packed yet",
        "shipped|delivered or not", "not shipped|dispatched|packed", "not yet|been shipped|dispatched|packed",
        "out for delivery", "in transit", "on its|the way", "left the warehouse",
        "track it|this|them|my|the|order|orders", "tracking", "locate", "current location",
    ),
    "track_eta": (
        "when .. arriv*|deliver*|reach*|come|comes|coming|ship|ships|shipped|shipping|dispatch*|expect*|here|due",
        "when .. get|receive|have it|them|my|the|this|that", "arrive", "arrives", "arriving", "arrival date|time",
        "expected|estimated arrival|delivery|date|time", "delivery date", "eta", "due date", "by when", "how soon",
        "expected on|by", "expect it|them|my|the|this", "how many more days", "how much longer", "be|get here",
        "delivery|delivered|arrive|arriving|estimated|expected when",
    ),
    "track_details": (
        "tracking|track|tracker number|no|num|id|code|link|url|detail|details|info|information|ref|reference",
        "tracker", "which|what company", "company .. delivering|handling|shipping", "attempt|attempts count|number|done|left",
        "awb", "consignment", "docket", "which|what|whose courier|carrier",
        "courier|carrier name|company|partner|service|detail|details|info", "name of the|my|your courier|carrier",
        "who is the|my courier|carrier", "delivery|shipping|logistics partner|company|service|agency",
        "who is|are delivering|shipping|bringing",
        "shipped|sent|coming with|via|through", "delivery attempt|attempts", "attempt|attempts made|so far",
        "how many attempt|attempts|times", "number of attempt|attempts", "attempted delivery|deliveries|to deliver",
        "tried|try|tries|trying to deliver", "tried|try delivering",
    ),
    "late_delivery": (
        "is|are|was|were|its|running|getting|so|very|really|already late", "late deliver*|arriv*|shipment|order",
        "deliver*|arriv*|coming|came|come|reach* late", "day|days|week|weeks late", "late by", "delay", "delays",
        "delayed", "delaying", "overdue", "should have arriv*|come|reach*|been|got|received|shipped",
        "should have .. by now", "supposed to arriv*|come|reach|be|get|have", "was|were due", "due yesterday",
        "past|passed|crossed|exceeded|missed the|its|due|delivery|expected|estimated|promised",
        "date has|is passed|gone|over|crossed|expired|lapsed", "date passed|expired|lapsed|crossed",
        "expected|estimated|delivery|due date .. passed|gone|over|crossed|expired|lapsed|missed", "expected yesterday",
        "still waiting", "been waiting",
        "waiting for|since", "taking|takes|took so|too|this|very long", "taking long|longer|forever|ages", "why .. so long",
        "been .. day|days|week|weeks|ages|forever", "not on time", "behind schedule",
    ),
    "not_received": (
        *(f"{no} {come}" for no in _NOT for come in _COME),
        "missing", "went missing", "lost in transit|shipping|delivery|post|mail", "got|is|was|been|gotten|get lost",
        "no sign of", "nowhere to be", "nothing has|was|is|came|arrived|delivered|received", "no delivery|package|parcel",
        "nobody|noone came|delivered|showed", "say*|show*|mark*|claim*|reflect* .. delivered",
        "did|has|have|is|was .. not|never arriv*|come|came|reach*|deliver*|receiv*|show*",
        "delivered but .. not|never|nothing", "delivered to someone|somebody|another|wrong",
        "delivered to .. wrong|else", "someone else .. receiv*|signed|took|got", "undelivered", "non delivery",
    ),
    "cancel": (
        "cancel*", "call off", "not want|need it|this|them|the|that", "no longer want|need|require",
        "changed my mind", "change of mind", "stop the|my|this order|delivery|shipment|shipping|dispatch",
        "stop shipping|sending|delivering|dispatching", "abort",
    ),
    "return": (
        "return*", "send|sending|sent|give|giving|take|taking it|them|this|that back", "send|sending|sent|give back",
        "pickup", "pick up", "picked up", "pick it|them up", "collect|collecting it|them|this|the|my",
    ),
    "exchange": (
        "exchang*", "swap*", "replace*", "different|another|other|bigger|smaller|larger|new|right|correct size|colour|color|fit",
        "size|colour|color exchange|swap|issue|problem", "not fit|fitting", "too big|small|large|tight|loose|baggy",
        "size up|down", "wrong fit|size|colour|color|shade", "need|want|get .. xs|s|m|l|xl|xxl|xxxl|small|medium|large",
        "xs|s|m|l|xl|xxl|small|medium|large instead", "instead of xs|s|m|l|xl|xxl|small|medium|large",
    ),
    "refund_status": (
        "refund* status|update|updates", "status|update|updates|news of|on|for|about .. refund*",
        "update|updates|news .. refund*", "where .. refund*|money", "when .. refund*|money",
        "where is|are my|the|that .. refund*|money|cash|amount",
        "when .. refund*|money arriv*|come|comes|coming|credited|reflect*|hit|hits|be|get|reach*|show*",
        "when will|do|can|should|would i|we get|receive|have|see|expect .. refund*|money",
        "refund* .. processed|initiated|credited|come|came|arrived|reflect*|received|done|completed|complete|pending"
        "|stuck|late|delayed|overdue|issued|sent|started|through",
        "refund* not|never|yet|still", "money not|never|still",
        "money|amount|refund* not|never|still .. credited|refunded|receiv*|come|came|returned|reflected|back",
        *(f"{no} {got} .. refund*|money" for no in _NOT
          for got in ("receiv*", "got", "get", "gotten", "come", "came", "credited", "seen", "see")),
        "has|have|is|was|did .. refund|refunds|refunded", "track .. refund*", "waiting .. refund*|money",
        "still|been waiting .. refund*|money", "refunded yet", "been refunded", "refund* yet",
        "late|delayed|overdue|pending|missing refund*", "not refunded|credited", "not yet|been refunded|credited",
        "never refunded|credited",
    ),
    "refund_terms": (
        "refund*", "reimburs*", "money back", "get|want|need|give my|the money", "how .. refund*|refunded|reimburs*",
        "will|would|do|can|could|shall i|we|you get|receive|have|expect|give|issue|process .. refund*|money",
        "eligible .. refund*", "refund* amount|method|mode|time|timeline|timelines|period|process|procedure|duration",
        "refund* to|into|in|on my|the|original|same|bank|card|upi|account|source|wallet", "full|partial|complete refund*",
        "refundable", "refund* .. take|takes|taking|long", "how long|soon|fast|quickly|much|many .. refund*",
        "how much time .. refund*|refunded|money", "how many days|weeks|hours .. refund*|refunded|money",
        "amount .. refund*|refunded", "faster|quicker|sooner|urgent|quick refund*",
        "refund* faster|sooner|quicker|quickly|urgently|asap", "store credit",
    ),
    "damaged_wrong": (
        "damag*", "broken", "torn", "tear", "ripped", "rip", "hole", "holes", "stain", "stains", "stained", "defect*",
        "faulty", "flaw*", "spoil*", "ruined", "dirty", "soiled", "scratch*", "crushed", "cracked", "smell*", "stink*",
        "faded", "discolo*",
        "missing button|buttons|piece|pieces|part|parts|tag|tags|thread|zip|zipper|hook|lace",
        "button|buttons|zip|zipper|stitch*|seam|seams|thread|threads .. missing|broken|loose|off|open|undone|coming",
        "poor|bad|cheap|terrible|horrible|worst|low|inferior quality", "quality issue|issues|problem|problems|complaint",
        "wrong item|items|product|products|order|thing|one|piece|design|pattern|print|fabric|material",
        "sent|send|gave|given|delivered|received|got|shipped|get .. wrong|different|incorrect",
        "wrong .. sent|delivered|received|arrived|came|shipped",
        "wrong size|colour|color|shade|fit sent|delivered|received|arrived|came|shipped|given",
        "sent|send|gave|given|delivered|received|got|shipped|get .. wrong size|colour|color|shade|fit",
        "not what i ordered|wanted|asked|bought|chose|selected", "not the .. i ordered|wanted|asked|bought|chose|selected",
        "not as described|shown|pictured|expected|advertised",
        "different from|than .. ordered|website|picture|photo|image|shown|described",
        "incorrect item|product|order|size|colour|color", "someone elses", "mismatch*", "already used|worn|opened",
        "second hand", "fake", "got|received|sent|delivered|came .. instead", "nothing like", "not like the|what",
        "looks|look different", "not match|look", "broke", "breaking", "came|fell|coming|falling apart",
    ),
    "address_change": (
        "delivery|shipping|new|wrong|different|another|correct|old|home|office|work|current|updated|alternate address",
        "address change|update|correction|edit|modification", "address is|was wrong|incorrect|different|changed|old|outdated",
        "address .. wrong|incorrect|mistake|typo",
        "change|changing|update|updating|edit|correct|correcting|modify|alter .. address|pincode|pin|location|city|landmark|flat|house",
        "change|changing|update|updating|edit|modify|alter where", "delivery|shipping location|destination|place|city|pincode",
        "deliver*|ship|send|sent .. different|another|new|other|somewhere|elsewhere", "deliver*|ship|send|sent .. instead",
        "different|another|new|other flat|house|place|city|location|pincode|building|street|area|apartment|floor",
        "redirect*", "rerout*", "re route",
        "moved|shifted|relocated|moving|shifting|relocating to", "new place|house|home|flat|apartment",
        "wrong pincode|pin|city|landmark|flat|house|street",
    ),
    "modify_order": (
        "change|changing|modify|modifying|edit|editing|alter|switch|amend .. size|colour|color|quantity|qty|order|item|variant|design",
        "add .. order", "add another|more|one", "remove .. from my|the|this order",
        "change|changing .. before dispatch|shipping|shipment|ship|ships",
        "wrong size|colour|color|quantity|item|variant .. selected|ordered|chose|picked|clicked",
        "increase|decrease|reduce|raise|lower .. quantity|qty|number|pieces", "quantity", "qty",
        "one more piece|pieces|item", "extra piece|pieces|item",
        "ordered|selected|chose|picked|clicked .. wrong size|colour|color|quantity|item|variant",
    ),
    "payment": (
        "how much did|have|was i|we pay|paid|charged", "what did|have i|we pay|paid", "how did|have i|we pay|paid",
        "how much was|is|were|got paid|charged|debited|deducted", "2|two|multiple|double charges|payments|debits|transactions|deductions",
        "breakdown", "breakup", "break up", "payment|transaction .. failed|declined|unsuccessful|pending|stuck",
        "amount correct|right|wrong|incorrect",
        "did i|we pay", "have i|we paid", "amount|money|total paid|charged|debited|deducted|billed",
        "paid by|with|via|using|through", "i|we paid",
        "payment status|detail|details|info|information|summary|confirmation|history|record|breakup|breakdown",
        "payment did|was|used", "which|what payment .. did|used|was|i",
        "payment method|mode|option|type did|was|used", "payment method|mode|option|type i|we used|chose",
        "charged|debited|deducted|billed|paid twice|double|again|extra|more|wrongly|multiple",
        "charged|debited|deducted|billed|paid .. twice|double|again|extra|multiple",
        "double|duplicate|multiple charge|charges|payment|payments|debit|debits|deduction|deductions"
        "|transaction|transactions|billing",
        "payment|transaction|txn failed|declined|unsuccessful|stuck|pending|issue|issues|problem|problems|error|errors"
        "|fail|failure|bounced|rejected",
        "failed|declined|unsuccessful|pending|stuck payment|transaction|txn",
        "money|amount|payment .. debited|deducted|cut|taken|gone", "debited|deducted|charged but", "overcharg*",
        "over charged", "charged more|extra|higher|wrong", "wrong|incorrect|excess amount|charge|charges",
        "unauthori*", "not authori*",
        "unknown|unrecogni*|suspicious|fraud*|unfamiliar charge|charges|transaction|transactions|payment|debit",
        "not recogni*", "charge|charges|transaction|payment|debit .. not recogni*|make|authori*",
        "did not make .. payment|transaction|purchase|order",
        "chargeback", "order total|amount|value", "total amount|cost|price|bill", "how much .. total|altogether",
        "what was the total",
    ),
    "invoice": (
        "invoice*", "bill", "bills", "receipt", "receipts", "gst", "tax document|details|bill",
        "where .. invoice*|bill|receipt",
        *(f"{no} {got} .. invoice*|bill|receipt" for no in _NOT for got in ("receiv*", "got", "get", "gotten", "come", "came")),
    ),
    "order_details": (
        "what did|have i|we order|ordered|buy|bought|purchase|purchased", "what i|we ordered|bought|purchased",
        "what was ordered|bought|purchased", "what is in|inside my|the|this order|package|parcel",
        "order|item|product|purchase detail|details|summary|info|information|confirmation",
        "details of|about|for my|the|this", "size|colour|color .. ordered|bought", "size|colour|color did i|we",
        "which|what size|colour|color did|is|was|were|have", "size|colour|color|price confirm|confirmation|check|needed|please|detail|details",
        "confirm|check .. size|colour|color|price|order", "price of|for my|the|this|it", "how much .. cost|costs|price|priced|worth",
        "how much was|is|were it|my|the|this", "cost of my|the|this",
    ),
    "speed_up": (
        "faster", "sooner", "quicker", "expedit*", "express", "speed up", "speed it up", "hurry", "rush", "prepone",
        "fast|quick|speedy|rapid|overnight|urgent|priority delivery|shipping|dispatch",
        "same|next|one day delivery|shipping|dispatch", "same day", "next day",
        "need|want|require it|them|this urgently|asap|fast|soon|quickly|early|earlier",
        "need|want|require it|them|this|that by|before|today|tomorrow|tonight|now",
        "need|want|require it|them|this|that .. today|tomorrow|tonight|asap|urgently|urgent|early|earlier|now",
        "deliver*|ship|send|dispatch .. urgently|asap|fast|soon|quickly|early|earlier",
        "make it early|earlier|faster|quick|quicker|sooner", "before the|its|my estimated|expected|due|delivery|promised",
        "earlier than", "any earlier", "in time for",
    ),
    "delivery_options": (
        "time slot|slots", "timeslot*", "slot", "slots", "delivery time slot|slots",
        "choose|select|pick|schedule|reschedul*|change|specific|particular|preferred|fixed|convenient .. time|date|day|slot|timing",
        "deliver*|come|bring .. evening|morning|afternoon|night|weekend|weekends|sunday|saturday|monday|tuesday"
        "|wednesday|thursday|friday|after|between",
        "evening|morning|afternoon|weekend|sunday|saturday delivery", "delivery timing|timings|hours|slot|slots|window",
        "what|which time", "delivery option|options|choice|choices|preference|preferences|instruction|instructions",
        "neighbo*", "security", "guard", "watchman", "reception", "receptionist", "concierge", "front door", "doorstep",
        "at the|my door|gate", "leave it|them|the|this|that|my", "left with|at", "drop it|them|the", "hand it|them over|to",
        "call|ring|phone|contact .. before deliver*|coming|arriv*", "when .. redeliver*|reattempt*", "when .. re deliver*|attempt*",
        "someone|somebody|anyone else .. receive|collect|accept|take|sign", "on my behalf", "family member",
        "not home|there|around", "not be home|available|there|around|present", "not at home", "be|am|was away",
        "out of town|station", "nobody|noone .. home|available|there|present", "no one .. home|available|there|present",
        "missed|miss|missing the|my|a delivery|courier|attempt", "failed|unsuccessful attempt|attempts|delivery",
        "failed|unsuccessful|missed delivery attempt|attempts", "delivery attempt|attempts failed|unsuccessful|missed",
        "delivery|attempt failed", "redeliver*", "re deliver*", "reattempt*", "re attempt*",
        "try|attempt|deliver|come|send again", "failed .. attempt", "another|next|second|third|2nd|3rd attempt", "reschedul*",
        "hold .. package|parcel|order|delivery",
    ),
    "human": (
        "human", "humans", "real|actual|live person|agent|human|people|someone", "live chat|support", "manager",
        "supervisor",
        "a|an|the|your|some|any|human|support|service|care|customer|senior agent|agents|executive|executives|representative"
        "|representatives|rep|operator|advisor|officer|person|staff",
        "agent|executive|representative|person|operator please|pls|plz|now|asap",
        "talk|speak|chat|connect|talking|speaking|connected to|with someone|somebody|anyone|support|customer|staff|team"
        "|your|a|an",
        "connect|transfer|put|forward|pass me|us|this to|through|with", "who can|do|should i|we talk|speak|contact|call|complain",
        "call me", "call back", "callback", "call from", "escalat*", "complaint", "complaints", "complain",
        "grievance", "raise|file|lodge|register|open|create|log a|an|the|my|new ticket|complaint|grievance|issue|case",
        "support ticket", "not a bot|robot|machine",
    ),
    # -- general topics: no item needed --
    "return_policy": (
        "return|returns|returning polic*", f"{_POLICY_ON} return*", "polic* .. return*",
        "accept|allow|take|offer|do|have returns", "returns accepted|allowed|possible|available|free",
        "free|easy returns", "return|returns charge|charges|fee|fees|cost|costs", "non returnable", "not returnable",
        "return rules|terms|conditions|guidelines",
    ),
    "refund_policy": (
        "refund|refunds polic*", f"{_POLICY_ON} refund*", "polic* .. refund*", "refunds work",
        "refund rules|terms|conditions|guidelines",
    ),
    "cancellation_policy": (
        "cancel* polic*", f"{_POLICY_ON} cancel*", "polic* .. cancel*",
        "cancel* charge|charges|fee|fees|penalty|rules|terms|conditions",
    ),
    "exchange_policy": (
        "exchang* polic*", f"{_POLICY_ON} exchang*", "polic* .. exchang*", "offer|do|allow|accept|have|provide exchang*",
        "exchang* available|possible|allowed|offered|rules|terms",
    ),
    "shipping_times": (
        "shipping|delivery|dispatch|transit|processing|handling time|times|timeline|timelines|duration|period|speed"
        "|estimate|estimates",
        "how long .. deliver*|ship|ships|shipping|shipped|dispatch*|arriv*|reach|reaches",
        "how many days .. deliver*|ship*|dispatch*|arriv*|reach*", "how much time .. deliver*|ship*|dispatch*|arriv*|reach*",
        "take long|time|days|weeks to ship|deliver|dispatch|arrive|reach|come", "long to ship|deliver|arrive",
        "wait|waiting time", "dispatch timing|timings|schedule",
        "how fast|quick|quickly|soon do|does|can you|saanjh deliver|ship|dispatch|send",
        "when do|will|does you|saanjh ship|dispatch|deliver|send", "standard delivery|shipping",
        "usually|normally|typically|generally take*|deliver*|ship*|arriv*|dispatch*",
    ),
    "shipping_charges": (
        "shipping|delivery|courier|postage|shipment|freight charge|charges|fee|fees|cost|costs|price|rate|rates|amount",
        "charge|charges|charged|fee|fees|cost for|on shipping|delivery", "pay for shipping|delivery",
        "cost of shipping|delivery",
        "free shipping|delivery|shipment", "shipping|delivery free|chargeable",
        "is shipping|delivery free|paid|chargeable|extra|included", "how much .. shipping|delivery|deliver|ship",
        "minimum order|purchase|cart", "free above|over",
    ),
    "cash_on_delivery": (
        "cash on delivery", "cod", "c o d", "pay|paying|payment on|upon|at|after delivery",
        "pay|paying at .. door|doorstep|home", "pay|paying cash", "pay|paying by|in|with cash", "cash payment|payments",
        "cash when|on|at|upon .. deliver*|arriv*|comes|receive",
        "pay|paying when .. deliver*|arriv*|receive|comes|get", "cash to .. courier|delivery",
    ),
    "international_shipping": (
        "international*", "overseas", "abroad", "outside india", "outside of india", "out of india",
        "outside|out the|of country", "out of the country", "other|another|foreign|different countr*", "worldwide",
        "globally", "global", "countries", "where do|can|will you deliver|ship|send",
        "which places|countries|cities|locations|areas|regions", "to the us|states|uk|uae|emirates|gulf",
        "germany|usa|uk|canada|australia|dubai|uae|singapore|nepal|europe|america|france|england|london|qatar|malaysia"
        "|bangladesh|china|japan|oman|kuwait|saudi|bahrain|maldives|thailand|ireland|italy|spain|bhutan",
    ),
    "payment_methods": (
        "payment method|methods|mode|modes|option|options|type|types|ways",
        "mode|modes|method|methods|ways of|for payment|paying", "way|ways to pay", "how can|do|should i|we pay",
        "which|what payment|payments", "payment|payments accepted|allowed|supported|available", "pay later",
        "paytm", "phonepe", "gpay", "google|apple|amazon pay", "paypal", "amex", "mastercard", "rupay",
        "support|supports|accept|accepts|take|takes .. mastercard|visa|amex|rupay|paytm|phonepe|gpay|paypal|upi|card|cards"
        "|wallet|wallets|emi|cheque|cash|crypto|netbanking",
        "how to pay", "pay|paying by|with|using|via|through", "do you accept|take|support",
        "accept|accepted|accepting .. card|cards|upi|paypal|wallet|wallets|cash|emi|cheque|crypto|payment|payments",
        "upi|card|cards|debit|credit|paypal|paytm|phonepe|gpay|wallet|wallets|emi|netbanking|banking|cheque|crypto .."
        " accepted|allowed|available|supported|work|works|ok|okay",
    ),
    "damaged_wrong_process": (
        "what if|should|happens|to .. damaged|defective|wrong|broken|faulty|torn",
        "if .. arrives|arrive|comes|come|is|gets|get|receive|received damaged|defective|wrong|broken|faulty|torn",
        "if i|we receive|get|received .. damaged|defective|wrong|broken|faulty",
        "in case .. damaged|defective|wrong|broken|faulty", "in case of damage|defect|defects|damages",
        "damaged|defective|wrong item|items|product|products|goods polic*|process|procedure",
        "polic* .. damaged|defective|wrong|damage|damages|defects", "process|procedure for|if .. damaged|defective|wrong",
    ),
    "how_to_track": (
        "how do|can|should|would|could|will i|we|you|one track", "how to track|trace",
        "how .. track .. order|orders|package|parcel|shipment|delivery|purchase",
        "how do|can|should|would|could i|we check|see|know|find|view|follow .. status|where|order|shipment|delivery"
        "|package|parcel",
        "where do|can|should|could i|we track|see|check|find|view|follow",
        "where do|can|should|could i|we track|see|check|find|view|follow .. status|order|orders|shipment|delivery|package|parcel",
        "track* link|page|website|site|portal|online|system|feature|option", "tracking work|works",
        "way|ways to track", "track an|any order|orders", "track orders", "my orders page|section", "can i|we track",
        "able to track", "guide|steps|process|procedure|link|app for|to .. track*",
    ),
    "support_contact": (
        "support|working|business|office|opening|service|helpdesk|care hours|timing|timings|times|days|schedule",
        "customer care|service|support", "contact|reach|email|mail|call|phone|message|whatsapp you|us|saanjh|support|someone",
        "write to you|us|saanjh|support", "how do|can|to|should i|we contact|reach|call|email|message",
        "contact detail|details|info|information|number|email|us|form|page",
        "phone|contact|mobile|whatsapp|helpline|toll|telephone number", "phone support", "email id|support",
        "email address", "helpline", "toll free", "whatsapp", "get in touch", "are you open|available|closed",
        "open on|till|until|from|for", "what time .. open|close|available", "can i call", "24 7", "24x7",
        "when are|is you|support|the .. open|available|working", "what time|when .. team|support|staff .. work|works|working|open",
        "support line|number|email|contact|team|hours", "available on|during|at", "contact option|options",
    ),
    "order_changes": (
        "polic* .. chang*|modif*|edit*|amend*", "chang*|modif*|edit*|amend* polic*",
        "change|changing|modify|modifying|edit|editing|amend .. after|once",
        "change|changing|modify|modifying|edit|editing|amend .. order|orders after|once",
        "changes|modifications|edits|amendments to|in|on .. order|orders",
        "order changes|modification|modifications|edits|amendment|amendments", "change|modify|edit|amend an|any order|orders",
        "can orders be", "orders be changed|edited|modified|amended", "editable",
    ),
    # -- small talk: answered only when the message is nothing else --
    "greeting": (
        "hi", "hii", "hiii", "hello", "helo", "hellow", "hey", "heya", "heyy", "hola", "namaste", "namaskar",
        "good morning|afternoon|evening|day", "greetings", "howdy", "sup", "hi|hello|hey there",
    ),
    "thanks": (
        "thanks", "thank you", "thankyou", "thank u", "thanx", "thx", "thnx", "thnks", "ty", "tysm",
        "thanks|thank .. help|support|assistance|info|information|time", "many thanks", "much appreciated",
        "appreciate it|that|your", "great help", "very helpful", "that helps|helped", "cheers",
    ),
    "goodbye": (
        "bye", "byee", "goodbye", "good bye", "bye bye", "see you|ya|u", "cya", "take care", "good night",
        "have a nice|good|great|lovely day|one|evening|night|weekend", "that is all|it", "that will be all",
        "thats all|it", "nothing else|more", "no more questions", "i am done", "im done", "all good",
        "done chatting|talking|here", "ending|end|close|closing this|the chat|conversation",
    ),
}
SMALL_TALK = ("greeting", "thanks", "goodbye")

# Common misspellings, put right at the start of a word before the phrases are tried.
_TYPOS = {
    "reciev": "receiv", "recive": "receive", "cancle": "cancel", "cansel": "cancel", "canel": "cancel",
    "retrun": "return", "retun": "return", "reutrn": "return", "refnd": "refund", "refudn": "refund",
    "refound": "refund", "adress": "address", "delievr": "deliver", "deliever": "deliver", "delivar": "deliver",
    "dilivery": "delivery", "delevery": "delivery", "exchnag": "exchang", "exchag": "exchang", "excahng": "exchang",
    "shiping": "shipping", "shiped": "shipped", "pakage": "package", "packge": "package", "invoce": "invoice",
    "invioce": "invoice", "reciept": "receipt", "recipt": "receipt", "damge": "damage", "dammag": "damag",
    "traking": "tracking", "arive": "arrive", "payed": "paid", "polcy": "policy", "hw": "how",
}
_FIXES = (
    (re.compile("|".join(rf"\b{wrong}" for wrong in _TYPOS)), lambda match: _TYPOS[match.group()]),
    (re.compile(r"\b(have|has|had|did|does|do|is|are|was|were|would|could|should|ca|wo)n'?t\b"), r"\1 not"),
    (re.compile(r"\b(where|what|when|how|who|it|that|there)'s\b|\b(where|what|when|how)s\b"), r"\1\2 is"),
    (re.compile(r"\bcannot\b"), "can not"),
    (re.compile(r"'"), ""),
    (re.compile(r"[-/]"), " "),
)
_GAP = r"((?: \w+){0,6})"
# The message is about one order of the customer's, so a general answer would miss the point.
_ONE_ORDER = re.compile(
    r"\b(?:my|this|that) (?:order|item|package|parcel|purchase|product)\b|\bi (?:ordered|bought|purchased)\b"
)


def _plain(message: str) -> str:
    """The message as the phrases expect it: lower case, contractions spelt out, one space between words."""
    text = message.lower().replace("’", "'")
    for pattern, replacement in _FIXES:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())


def _phrase(phrase: str) -> re.Pattern:
    words = []
    for word in phrase.split():
        either = "|".join(re.escape(one).replace(r"\*", r"\w*") for one in word.split("|"))
        words.append(_GAP if word == ".." else f"(?:{either})")
    # A gap brings its own leading spaces, so no space is put before it.
    return re.compile(r"(?<!\w)" + " ".join(words).replace(" " + _GAP, _GAP) + r"(?!\w)")


_PHRASES = [(_phrase(phrase), topic) for topic, phrases in TOPICS.items() for phrase in phrases]


def fmt_date(day: str) -> str:
    """'2026-11-24' -> 'November 24'."""
    parsed = date.fromisoformat(day)
    return f"{parsed:%B} {parsed.day}"


def fmt_inr(amount: int) -> str:
    """1499 -> '₹1,499'."""
    # ponytail: thousands grouping, which matches Indian grouping only below ₹1,00,000. No fixture price
    # comes close; switch to lakh grouping if larger amounts appear.
    return f"₹{amount:,}"


def ticket_reply(ref: str, verified: bool = True) -> str:
    if verified:
        return f"I have passed this to our support team. Your reference is {ref}."
    return f"I could not verify an answer just now, so I have passed this to our support team. Your reference is {ref}."


def topics(message: str, quick_action: str | None = None) -> list[str]:
    """Every help topic the message raises, in the order the customer raised them. A quick action stands alone.

    Where two phrases of different topics overlap, the one with more words of its own wins: "where is my refund"
    is a refund question, not a tracking one. Small talk counts only when the message is little else.
    """
    if quick_action in QUICK:
        return [QUICK[quick_action]]
    text = _plain(message)
    hits = []  # (letters matched outside the gaps, start, end, topic)
    for pattern, topic in _PHRASES:
        for match in pattern.finditer(text):
            gaps = sum(len(gap) for gap in match.groups())
            hits.append((match.end() - match.start() - gaps, match.start(), match.end(), topic))
    kept = []
    # The heaviest first; of two equally heavy, the tighter one (its end comes sooner), then the earlier one.
    for hit in sorted(hits, key=lambda found: (-found[0], found[2] - found[1], found[1])):
        _, start, end, topic = hit
        if all(topic == other or end <= begins or start >= ends for _, begins, ends, other in kept):
            kept.append(hit)
    kept.sort(key=lambda found: found[1])
    found = list(dict.fromkeys(topic for _, _, _, topic in kept))
    asked = [topic for topic in found if topic not in SMALL_TALK]
    if asked:
        return asked
    # ponytail: "little else" is at most eight words outside the small-talk phrases and no question among them,
    # so "Hi, do you have this in red?" and "Hello, any discounts?" are not greetings to answer with a greeting.
    rest = text
    for _, start, end, _ in reversed(kept):
        rest = rest[:start] + rest[end:]
    words = re.findall(r"\w+", rest)
    return found if len(words) <= 8 and not (words and "?" in rest) else []


def needs_item(found: list[str], message: str, named: int = 0) -> bool:
    """True when the answer depends on which item is meant, so the customer has to choose one.

    A request for a person never waits for that. An order topic needs its item when it has no general answer,
    or when the message is about one order: it names items (`named` of them) or points at one ("my order").
    """
    # ponytail: "points at one order" is a short pattern. "Can I cancel it?" with nothing selected gets the
    # cancellation policy instead of the question which item is meant. Upgrade path: the intent classifier.
    order = [topic for topic in found if isinstance(DRAFTS[topic], dict)]
    if not order or "human" in found:
        return False
    return bool(named or _ONE_ORDER.search(_plain(message))) or any(None not in DRAFTS[topic] for topic in order)


def match_items(message: str, items: list[dict]) -> list[dict]:
    """The items the message names as whole words: by full name when any full name appears, otherwise by noun."""
    # ponytail: literal matching. Plurals, typos and descriptions ("the blue one") match nothing, so the
    # customer is asked to choose. Upgrade path: fuzzy matching over the customer's own items.
    def named(words: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(words)}(?!\w)", message, re.IGNORECASE) is not None

    # "my blue kurta" is one item even though another kurta shares its noun.
    return [item for item in items if named(item["name"])] or [item for item in items if named(item["noun"])]


# The two oldest policy rules still name the quick actions' intents where the newer ones name help topics.
_LEGACY = {"not_received": "missing", "refund_status": "refund", "refund_terms": "refund"}


def mandated(found: list[str], item: dict | None, policy: dict) -> dict | None:
    """The first backend-enforced escalation rule that holds for one of the topics, as {"rule", "summary"}.

    A rule's "when" lists what must hold: "intent" is the topic, "refund_overdue" whether the item's refund is
    overdue, and any other key is an item fact by its name. A list means one of its values. A rule that needs
    an item fact never holds without an item.
    """
    facts = {**item, "refund_overdue": bool(item["refund"] and item["refund"]["overdue"])} if item else {}
    for rule in policy["escalation"]:
        if rule["enforced_by"] != "backend":
            continue
        for topic in found:
            context = {**facts, "intent": topic}
            if all(
                key in context
                and (context[key] in _one_of(wanted) or key == "intent" and _LEGACY.get(topic) in _one_of(wanted))
                for key, wanted in rule["when"].items()
            ):
                about = f" on {item['name']} ({item['id']})" if item else ""
                return {"rule": rule["id"], "summary": f"Backend rule {rule['id']}{about}: {rule['text']}"}
    return None


def _one_of(wanted: object) -> list:
    return wanted if isinstance(wanted, list) else [wanted]


# ---- Drafts ---------------------------------------------------------------------------------------------------
# Everything a draft can say. A sentence is either quoted from the policy, "policy.<section>.<sentence number>",
# or written here as (text, the record fields and policy sections it relies on...). {It} is "Your kurta" the
# first time a draft uses it and "It" afterwards; the other fields are listed in _fields.
SENTENCES: dict[str, str | tuple[str, ...]] = {
    # the state of the item
    "packed": ("{It} {is} packed and {has} not shipped yet.", "item.status"),
    "shipped": ("{It} {has} shipped.", "item.status"),
    "delivered": ("{It} {was} delivered{on_delivered}.", "item.status", "item.delivered_at"),
    "cancelled": ("{It} {has} been cancelled.", "item.status"),
    "return_requested": (
        "{It} {was} delivered{on_delivered}, and a return was requested{on_requested}.",
        "item.status", "item.delivered_at", "item.return"),
    "refund_processing": ("A refund for your {noun} is being processed.", "item.status"),
    "returned": ("{It} {was} returned, and {its} refund is being processed.", "item.status", "item.refund"),
    "undelivered": ("{It} {has} been cancelled, so {it} will not be delivered.", "item.status"),
    # what comes next
    "eta_due": ("{It} {is} expected on {eta}.", "item.eta"),
    "eta_passed": ("{It} {was} expected on {eta} and {has} not been delivered yet.", "item.eta"),
    "pickup_due": ("Pickup is scheduled for {pickup_on}.", "item.return"),
    "picked_up": ("The return has been picked up.", "item.return"),
    "refund_initiated": (
        "The refund of {amount}{via} was initiated on {initiated} and is expected by {expected_by}.", "item.refund"),
    "refund_overdue": (
        "The refund of {amount}{via} was initiated on {initiated} and was expected by {expected_by}, so it is overdue.",
        "item.refund"),
    "refund_credited": ("The refund of {amount}{via} was credited{on_credited}.", "item.refund"),
    # delivery
    "not_late": ("{It} {is} not late: {its} estimated delivery date has not passed.", "item.eta", "policy.late_delivery"),
    "delivered_on_eta": (
        "{It} {was} delivered{on_delivered}, {its} estimated delivery date.",
        "item.status", "item.delivered_at", "item.eta"),
    "delivered_early": (
        "{It} {was} delivered{on_delivered}, before {its} estimated delivery date of {eta}.",
        "item.status", "item.delivered_at", "item.eta"),
    "delivered_late": (
        "{It} {was} delivered{on_delivered}, {days_late} after {its} estimated delivery date of {eta}.",
        "item.status", "item.delivered_at", "item.eta"),
    "in_transit": "policy.missing_delivery.0",
    "scan": ("The courier scan shows your {noun} as delivered{on_delivered}.", "item.status", "item.delivered_at"),
    "dispute": "policy.missing_delivery.1",
    "shipment": (
        "{It} {was} shipped with {courier}, tracking number {tracking}.", "item.courier", "item.tracking_number"),
    "couriers": "policy.tracking.1",
    "no_tracking": "policy.tracking.3",
    "never_shipped": (
        "{It} never shipped, so {it} {has} no courier or tracking number.", "item.shipped_at", "item.tracking_number"),
    "tracking_sent": "policy.tracking.2",
    "attempts": ("Delivery attempts so far: {attempts}.", "item.delivery_attempts"),
    "no_express": "policy.delivery_changes.0",
    "no_slot": "policy.delivery_changes.1",
    "no_neighbour": "policy.delivery_changes.2",
    "hours": "policy.delivery_attempts.0",
    "retry": "policy.delivery_attempts.1",
    # cancelling
    "cancel_confirm": ("{It} can still be cancelled: please confirm to cancel {them}.", "policy.cancellation"),
    "cancel_possible": ("{It} can still be cancelled.", "policy.cancellation"),
    "cancel_refund": (
        "If you cancel {them}, {its} full price of {price} is refunded to the original payment method.",
        "item.price_inr", "policy.cancellation"),
    "nothing_paid": "policy.cancellation.2",
    "no_cancel_shipped": (
        "{It} shipped{on_shipped}, so {it} can no longer be cancelled.",
        "item.status", "item.shipped_at", "policy.cancellation"),
    "no_cancel": (
        "{It} can no longer be cancelled, because an item can be cancelled only until it ships.", "policy.cancellation"),
    "already_cancelled": ("{It} {has} already been cancelled.", "item.status"),
    # returning
    "return_after_delivery": (
        "Once {it} {is} delivered, {it} can be returned within {days} days of the delivery date.", "policy.returns"),
    "not_returnable": ("{It} cannot be returned{because}.", "item.non_returnable_reason", "policy.returns"),
    "return_confirm": (
        "{It} can be returned until {deadline}: please confirm to request the return.",
        "item.return_deadline", "policy.returns"),
    "return_possible": ("{It} can be returned until {deadline}.", "item.return_deadline", "policy.returns"),
    "return_closed": (
        "{Its} return window closed on {deadline}, so {it} can no longer be returned.",
        "item.return_deadline", "policy.returns"),
    "return_conditions": "policy.returns.2",
    "return_pickup": "policy.returns.4",
    "return_refund": "policy.returns.5",
    "return_cod": "policy.returns.6",
    "return_confirmed": ("Your return of the {noun} is confirmed.", "item.return"),
    # exchanging and changing
    "no_exchange": "policy.exchanges.0",
    "exchange_by_return": "policy.exchanges.1",
    "no_exchange_either": "policy.exchanges.2",
    "exchange_by_cancel": (
        "To get a different size or colour, cancel the item before it ships and place a new order.",
        "policy.order_changes"),
    "reorder": ("To get a different size or colour, place a new order.", "policy.exchanges"),
    "no_edit": "policy.order_changes.0",
    "edit_by_cancel": "policy.order_changes.1",
    "edit_by_return": "policy.order_changes.2",
    "address_rule": "policy.address_change.0",
    "address_ticket": "policy.address_change.1",
    "address_after": "policy.address_change.2",
    # refunds
    "no_refund": ("There is no refund on record for {them}.", "item.refund"),
    "refund_if_return": (
        "If you return {them}, the refund of {its} price, {price}, is initiated once the item passes inspection.",
        "item.price_inr", "policy.refunds"),
    "refund_days": "policy.refunds.0",
    "refund_cod": "policy.refunds.3",
    "fees_kept": "policy.refunds.4",
    # payment, invoice and the order itself
    "paid_prepaid": (
        "The order with your {noun}, {order_id}, came to {total}, paid by {paid_by} when it was placed.",
        "item.order_total_inr", "item.payment_method", "policy.payment_methods"),
    "paid_cod": (
        "The order with your {noun}, {order_id}, came to {total}, payable to the courier in cash on delivery.",
        "item.order_total_inr", "item.payment_method", "policy.cash_on_delivery"),
    "item_price": ("Of that, {price} is the price of the {noun}.", "item.price_inr"),
    "fees": ("The total includes {fees}.", "item.shipping_fee_inr", "item.cod_fee_inr"),
    "payment_problem": "policy.payment_problems.0",
    "invoice_sent": (
        "The tax invoice for your {noun} was emailed to you when {it} shipped{on_shipped}; it can also be"
        " downloaded under My Orders on our website.", "item.shipped_at", "policy.invoices"),
    "no_invoice": "policy.invoices.2",
    "invoice_on_ship": "policy.invoices.0",
    "details": (
        "You ordered the {name} ({variant}) for {price} on {placed}, in order {order_id}.",
        "item.name", "item.variant", "item.price_inr", "item.placed_at", "item.order_id"),
    "pay_method": ("The payment method is {paid_by}.", "item.payment_method"),
    # two actions asked for at once, and small talk
    "which_action": ("Which one would you like to go ahead with: a cancellation or a return?",),
    "hello": ("Hello! How can I help you today?",),
    "welcome": ("You're welcome! Is there anything else I can help you with?",),
    # The live gate scored a goodbye that only wished the customer well as unsupported; this one rests on the policy.
    "bye": ("Goodbye! This assistant is available at any time if you need anything else.", "policy.support"),
}

ANY = "*"
# Which sentences answer a topic. A string is an answer that needs no item. A table gives the answer by the
# item's status (ANY: every other status) and, under None, the general answer when no item is meant; a table
# without None needs an item. A word is a sentence, a whole policy section ("policy.returns"), or one of the
# CHOICES below, which pick sentences by what the record says.
DRAFTS: dict[str, str | dict[str | None, str]] = {
    "track_status": {ANY: "state next attempted"},
    "track_eta": {"cancelled": "undelivered next", ANY: "state next attempted", None: "policy.shipping_times"},
    "track_details": {"packed": "state shipping tracking_sent", ANY: "state shipping tried"},
    "late_delivery": {"packed": "state next on_time", "shipped": "state next attempted on_time",
                      "delivered": "arrived dispute", "cancelled": "undelivered next", ANY: "state next"},
    "not_received": {"shipped": "state next attempted in_transit", "delivered": "scan dispute",
                     "cancelled": "undelivered next", ANY: "state next"},
    "cancel": {"packed": "state cancel", "shipped": "no_cancel_shipped returnable", "delivered": "state no_cancel returnable",
               "cancelled": "already_cancelled next", ANY: "state no_cancel next", None: "policy.cancellation"},
    "return": {"packed": "state returnable cancel", "shipped": "state returnable",
               "delivered": "state returnable return_how", "return_requested": "state next return_refund",
               ANY: "state next", None: "policy.returns"},
    "exchange": {"packed": "state no_exchange exchange_by_cancel", "shipped": "state no_exchange exchange",
                 "delivered": "state no_exchange exchange", "return_requested": "state no_exchange exchange_by_return",
                 ANY: "state no_exchange reorder", None: "policy.exchanges"},
    "refund_status": {"return_requested": "state next refund_terms", ANY: "state refund_terms"},
    "refund_terms": {"return_requested": "state next refund_terms", ANY: "state refund_terms", None: "policy.refunds"},
    "damaged_wrong": {ANY: "policy.damaged_or_wrong_item"},
    "address_change": {"packed": "state address_rule address_ticket", "cancelled": "undelivered next",
                       ANY: "state address_after", None: "policy.address_change"},
    "modify_order": {"packed": "state no_edit edit_by_cancel", "shipped": "state no_edit edit_by_return",
                     "delivered": "state no_edit returnable", "return_requested": "state no_edit edit_by_return",
                     ANY: "state no_edit", None: "policy.order_changes"},
    "payment": {"cancelled": "state paid refund payment_problem", ANY: "paid refund payment_problem"},
    "invoice": {ANY: "invoice", None: "policy.invoices"},
    "order_details": {ANY: "details pay_method state next"},
    "speed_up": {"packed": "state next no_express", "shipped": "state next no_express", "cancelled": "undelivered next",
                 ANY: "state no_express", None: "no_express"},
    "delivery_options": {"packed": "next no_slot no_neighbour hours retry",
                         "shipped": "next no_slot no_neighbour hours retry", "cancelled": "undelivered next",
                         ANY: "state no_slot no_neighbour", None: "no_slot no_neighbour hours retry"},
    "human": "policy.support",
    "return_policy": "policy.returns",
    "refund_policy": "policy.refunds",
    "cancellation_policy": "policy.cancellation",
    "exchange_policy": "policy.exchanges",
    "shipping_times": "policy.shipping_times",
    "shipping_charges": "policy.shipping_charges",
    "cash_on_delivery": "policy.cash_on_delivery",
    "international_shipping": "policy.international_shipping",
    "payment_methods": "policy.payment_methods",
    "damaged_wrong_process": "policy.damaged_or_wrong_item",
    "how_to_track": "policy.tracking",
    "support_contact": "policy.support",
    "order_changes": "policy.order_changes policy.address_change",
    "greeting": "hello",
    "thanks": "welcome",
    "goodbye": "bye",
}
STATUSES = ("packed", "shipped", "delivered", "cancelled", "return_requested", "refund_processing")


def _state(item: dict, offer: str | None = None) -> list[str]:
    """Where the item stands. A refund that follows a return says so, when the record gives the refund's reason."""
    returned = item["status"] == "refund_processing" and (item["refund"] or {}).get("reason") == "return"
    return ["returned" if returned else item["status"]]


def _arrived(item: dict, offer: str | None = None) -> list[str]:
    """A delivered item against its estimated delivery date, when the record holds both."""
    delivered, eta = item["delivered_at"], item["eta"]
    if not (delivered and eta):
        return ["delivered"]
    return ["delivered_late" if delivered > eta else "delivered_early" if delivered < eta else "delivered_on_eta"]


def _refund(item: dict, offer: str | None = None) -> list[str]:
    """The refund on record, if there is one."""
    refund = item["refund"]
    if not refund:
        return []
    return ["refund_credited" if refund["status"] == "credited" else
            "refund_overdue" if refund["overdue"] else "refund_initiated"]


def _next(item: dict, offer: str | None = None) -> list[str]:
    """What the record says comes next: the delivery, the pickup of a return, or the refund."""
    if item["status"] in ("packed", "shipped"):
        return ["eta_passed" if item["late"] else "eta_due"] if item["eta"] else []
    pickup = item["return"] if item["status"] == "return_requested" else None
    if pickup:
        return ["picked_up"] if pickup["pickup_status"] == "picked_up" else ["pickup_due"] if pickup["pickup_on"] else []
    return _refund(item)


def _returnable(item: dict, offer: str | None = None) -> list[str]:
    """What the record says about returning the item, with the offer when that is what the customer asked for."""
    if not item["returnable"]:
        return ["not_returnable"]
    if item["status"] in ("packed", "shipped"):
        return ["return_after_delivery"]
    if item["can_return"]:
        return ["return_confirm"] if offer == "return" else ["return_possible", *([] if item["prepaid"] else ["return_cod"])]
    return ["return_closed"] if item["return_deadline"] else []


def _open(item: dict) -> bool:
    """A return is still ahead of the item: it is returnable and its window has not closed."""
    return item["returnable"] and (item["can_return"] or not item["return_deadline"])


def _cancel(item: dict, offer: str | None = None) -> list[str]:
    if "cancel" not in item["permitted_actions"]:
        return ["no_cancel"]
    return ["cancel_confirm" if offer == "cancel" else "cancel_possible",
            "cancel_refund" if item["prepaid"] else "nothing_paid"]


def _exchange(item: dict, offer: str | None = None) -> list[str]:
    how = _returnable(item)
    return ["exchange_by_return", *how] if _open(item) else [*how, "no_exchange_either"]


def _refund_terms(item: dict, offer: str | None = None) -> list[str]:
    """The refund on record; without one, how a refund would come about for this item and how it is paid."""
    if item["refund"]:
        return _refund(item)
    paid_back = ["refund_days", *(["fees_kept"] if item["shipping_fee_inr"] else [])]
    status, prepaid = item["status"], item["prepaid"]
    if status in ("packed", "cancelled"):
        terms = ["nothing_paid"] if not prepaid else ["cancel_refund", *paid_back] if status == "packed" else []
    elif status in ("shipped", "delivered"):
        how = _returnable(item)
        paid = ["refund_if_return", *paid_back] if prepaid else [] if "return_cod" in how else ["refund_cod"]
        terms = [*how, *(paid if _open(item) else [])]
    else:
        terms = ["return_refund", *(paid_back if prepaid else ["refund_cod"])] if status == "return_requested" else []
    return ["no_refund", *terms]


def _paid(item: dict, offer: str | None = None) -> list[str]:
    if not item["prepaid"] and item["status"] == "cancelled":
        return ["nothing_paid"]
    return ["paid_prepaid" if item["prepaid"] else "paid_cod",
            *(["item_price"] if item["order_total_inr"] != item["price_inr"] else []),
            *(["fees"] if item["shipping_fee_inr"] or item["cod_fee_inr"] else [])]


def _unshipped(item: dict) -> bool:
    return item["status"] == "packed" or item["status"] == "cancelled" and not item["shipped_at"]


def _invoice(item: dict, offer: str | None = None) -> list[str]:
    if _unshipped(item):
        return [item["status"], "no_invoice", *(["invoice_on_ship"] if item["status"] == "packed" else [])]
    return ["invoice_sent"]


def _shipping(item: dict, offer: str | None = None) -> list[str]:
    """The courier and tracking number; for an item that has not shipped, why there is none."""
    if item["courier"] and item["tracking_number"]:
        return ["shipment"]
    if not _unshipped(item):
        return []
    return ["never_shipped"] if item["status"] == "cancelled" else ["couriers", "no_tracking"]


CHOICES = {
    "state": _state,
    "arrived": _arrived,
    "next": _next,
    # A failed delivery attempt is the news about an item in transit, with what the courier does next.
    "attempted": lambda item, offer: (
        ["attempts", "retry"] if item["status"] == "shipped" and item["delivery_attempts"] else []),
    "refund": _refund,
    "refund_terms": _refund_terms,
    "returnable": _returnable,
    "return_how": lambda item, offer: ["return_conditions", "return_pickup", "return_refund"] if offer == "return" else [],
    "cancel": _cancel,
    "exchange": _exchange,
    "paid": _paid,
    "invoice": _invoice,
    "shipping": _shipping,
    "tried": lambda item, offer: [] if _unshipped(item) or item["delivery_attempts"] is None else ["attempts"],
    "on_time": lambda item, offer: ["not_late"] if item["eta"] and not item["late"] else [],
}


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.?!]) ", text)


def _names(row: str, item: dict | None, offer: str | None, policy: dict) -> list[str]:
    """The sentences a row of DRAFTS stands for, by name."""
    names = []
    for word in row.split():
        if word in CHOICES:
            names += CHOICES[word](item, offer)
        elif word.startswith("policy.") and word.count(".") == 1:  # a whole section
            names += [f"{word}.{number}" for number in range(len(_sentences(policy[word[7:]]["text"])))]
        else:
            names.append(word)
    return names


def _source(name: str) -> str | tuple[str, ...]:
    return SENTENCES.get(name, name)  # a row may also quote the policy directly


def _evidence(name: str) -> tuple[str, ...]:
    source = _source(name)
    return (source.rpartition(".")[0],) if isinstance(source, str) else source[1:]


def _spoken(method: str | None) -> str | None:
    """'Credit card' -> 'credit card', as it reads inside a sentence; 'UPI' stays."""
    return method if not method or method.isupper() else method.lower()


def _fields(item: dict, policy: dict) -> dict:
    """What the sentences are filled in from. A field the record does not hold is used by no chosen sentence."""
    # ponytail: a noun that ends in "s" but not "ss" is read as plural (chinos, sneakers; not dress). An odd
    # noun gets the wrong verb form. Upgrade path: a number field on the item.
    plural = item["noun"].endswith("s") and not item["noun"].endswith("ss")
    refund, pickup = item["refund"] or {}, item["return"] or {}

    def day(value: str | None) -> str | None:
        return fmt_date(value) if value else None

    def on(value: str | None) -> str:
        return f" on {fmt_date(value)}" if value else ""

    reason = policy["returns"].get("non_returnable", {}).get(item["non_returnable_reason"])
    late = (date.fromisoformat(item["delivered_at"]) - date.fromisoformat(item["eta"])).days if (
        item["delivered_at"] and item["eta"]) else 0
    fees = [f"{fmt_inr(item['shipping_fee_inr'])} for shipping"] if item["shipping_fee_inr"] else []
    fees += [f"a {fmt_inr(item['cod_fee_inr'])} cash-on-delivery fee"] if item["cod_fee_inr"] else []
    return {
        **{key: item[key] for key in ("noun", "name", "variant", "order_id", "courier")},
        "It": "They" if plural else "It", "it": "they" if plural else "it", "them": "them" if plural else "it",
        "Its": "Their" if plural else "Its", "its": "their" if plural else "its", "is": "are" if plural else "is",
        "has": "have" if plural else "has", "was": "were" if plural else "was",
        "eta": day(item["eta"]), "placed": day(item["placed_at"]), "deadline": day(item["return_deadline"]),
        "days_late": f"{late} day{'' if late == 1 else 's'}",
        "on_shipped": on(item["shipped_at"]), "on_delivered": on(item["delivered_at"]),
        "price": fmt_inr(item["price_inr"]), "total": fmt_inr(item["order_total_inr"]),
        "paid_by": _spoken(item["payment_method"]), "fees": " and ".join(fees),
        "tracking": item["tracking_number"], "attempts": item["delivery_attempts"],
        "days": policy["returns"]["window_days"], "because": f", because {reason}" if reason else "",
        "amount": fmt_inr(refund["amount_inr"]) if refund else None,
        "via": f" via {_spoken(refund['method'])}" if refund.get("method") else "",
        "initiated": day(refund.get("initiated_at")), "expected_by": day(refund.get("expected_by")),
        "on_credited": on(refund.get("credited_at")),
        "on_requested": on(pickup.get("requested_at")), "pickup_on": day(pickup.get("pickup_on")),
    }


def _render(names: list[str], item: dict | None, policy: dict) -> list[str]:
    """The sentences in words. The first one about the item names it; the later ones say "it"."""
    fields, texts, named = (_fields(item, policy) if item else {}), [], False
    for name in names:
        source = _source(name)
        if isinstance(source, str):
            _, section, number = source.split(".")
            texts.append(_sentences(policy[section]["text"])[int(number)])
            continue
        first = "{It}" in source[0] and not named
        named = named or first
        texts.append(source[0].format(**{**fields, **({"It": f"Your {fields['noun']}"} if first else {})}))
    return texts


def candidate(found: list[str], item: dict | None, policy: dict) -> dict | None:
    """The routine draft that answers the topics, in their order: {"draft", "evidence", "action"}, or None.

    None when no topic was found, when a topic cannot be answered without an item, or when the item is in a
    status the tables do not know. No sentence is said twice. At most one action is offered, and only when the
    customer asked for it and the record permits it; asked for both at once, the draft offers neither and asks.
    """
    if item and item["status"] not in STATUSES:
        return None
    found = found[:MAX_TOPICS]
    asked = [topic for topic in found if topic in ACTIONS]
    offer = asked[0] if len(asked) == 1 and item and asked[0] in item["permitted_actions"] else None
    parts, said = [], set()
    for topic in found:
        row = DRAFTS[topic]
        if isinstance(row, dict):
            row = row.get(item["status"], row.get(ANY)) if item else row.get(None)
        if row is None:
            return None
        parts.append([name for name in dict.fromkeys(_names(row, item, offer, policy)) if name not in said])
        said.update(parts[-1])
    if len(asked) == 2 and item and item["permitted_actions"]:
        parts.append(["which_action"])

    def names() -> list[str]:
        return [name for part in parts for name in part]

    # A single topic is answered in full. Combined parts lose their last sentences, the longest part first,
    # until the draft is short; every part keeps its first two sentences, which carry the answer and any offer.
    while len(parts) > 1 and len(" ".join(_render(names(), item, policy)).split()) > MAX_WORDS:
        long = [part for part in parts if len(part) > 2]
        if not long:
            break
        max(long, key=len).pop()
    if not names():
        return None
    return {
        "draft": " ".join(_render(names(), item, policy)),
        "evidence": list(dict.fromkeys(ref for name in names() for ref in _evidence(name))),
        "action": offer,
    }


def confirmation_reply(action: str, done: bool, item: dict, policy: dict, today: str) -> str:
    """What the customer is told after confirming: what was done, or the state that now prevents it.

    `today` is part of the signature the store calls; the record already says what depends on the date.
    """
    if not done:
        what = "cancellation" if action == "cancel" else "return"
        return (
            f"This {what} was not carried out because the order changed after it was offered. "
            + candidate([action], item, policy)["draft"]
        )
    names = ["cancelled", *_refund(item)] if action == "cancel" else ["return_confirmed", "pickup_due", "return_refund"]
    return " ".join(_render(names, item, policy))
