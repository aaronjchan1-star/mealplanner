"""
Static reference data used in the settings form.

NSW_SUBURBS: a curated list of suburbs and locality names used for the
location-field datalist autocomplete. Not exhaustive — Sydney metro is
well-covered, regional NSW gets the bigger towns. Users can still type a
suburb that isn't in the list; this just helps with typos and discovery.

KNOWN_SUPERMARKETS: chains that get supermarket checkboxes in settings.
Anything else goes in the free-text "Other" field.

KITCHEN_APPLIANCES: appliance options for the plan form so the AI knows
what's available (and so it can prefer recipes that use them).
"""

KNOWN_SUPERMARKETS = [
    "Woolworths",
    "Coles",
    "Aldi",
    "IGA",
    "Harris Farm",
    "Costco",
    "Independent / local grocer",
    "Asian grocer",
    "Indian grocer",
    "Butcher",
    "Fish market",
]

KITCHEN_APPLIANCES = [
    "oven",
    "stovetop",
    "microwave",
    "air fryer",
    "slow cooker",
    "pressure cooker",
    "rice cooker",
    "blender",
    "food processor",
    "BBQ",
]

# Common NSW suburbs and localities. Not exhaustive, but covers the bulk of
# Sydney metro plus the main regional centres. Used as a <datalist> — the
# field accepts free text, the list just provides type-ahead suggestions.
NSW_SUBURBS = sorted(set([
    # Sydney CBD and inner
    "Sydney", "The Rocks", "Pyrmont", "Ultimo", "Haymarket", "Surry Hills",
    "Darlinghurst", "Potts Point", "Woolloomooloo", "Kings Cross", "Elizabeth Bay",
    "Rushcutters Bay", "Paddington", "Redfern", "Waterloo", "Zetland",
    "Alexandria", "Erskineville", "Newtown", "Camperdown", "Glebe",
    "Forest Lodge", "Annandale", "Leichhardt", "Lilyfield", "Rozelle",
    "Balmain", "Birchgrove", "Drummoyne", "Five Dock", "Russell Lea",
    # Eastern suburbs
    "Bondi", "Bondi Beach", "Bondi Junction", "Tamarama", "Bronte",
    "Coogee", "Clovelly", "Randwick", "Kensington", "Kingsford",
    "Maroubra", "Malabar", "Little Bay", "Matraville", "Pagewood",
    "Eastlakes", "Mascot", "Botany", "Rosebery", "Beaconsfield",
    "Double Bay", "Bellevue Hill", "Point Piper", "Rose Bay",
    "Vaucluse", "Dover Heights", "Watsons Bay", "Edgecliff", "Woollahra",
    # Inner west
    "Marrickville", "Dulwich Hill", "Petersham", "Stanmore", "Enmore",
    "St Peters", "Tempe", "Sydenham", "Hurlstone Park", "Earlwood",
    "Canterbury", "Ashbury", "Ashfield", "Croydon", "Croydon Park",
    "Burwood", "Strathfield", "Strathfield South", "Homebush", "Homebush West",
    "Concord", "Concord West", "Cabarita", "Mortlake", "Rhodes",
    "Liberty Grove", "North Strathfield",
    # West / Parramatta corridor
    "Lidcombe", "Auburn", "Berala", "Regents Park", "Granville",
    "Clyde", "South Granville", "Guildford", "Yennora", "Fairfield",
    "Cabramatta", "Canley Vale", "Canley Heights", "Bonnyrigg",
    "Parramatta", "Harris Park", "Rosehill", "Camellia", "Westmead",
    "North Parramatta", "Northmead", "Wentworthville", "Pendle Hill",
    "Toongabbie", "Old Toongabbie", "Girraween", "Greystanes", "Pemulwuy",
    "Merrylands", "South Wentworthville", "Mays Hill",
    # Sydney Olympic Park / Homebush Bay
    "Newington", "Sydney Olympic Park", "Wentworth Point", "Silverwater",
    "Lewisham", "Summer Hill", "Haberfield",
    # Hills district
    "Baulkham Hills", "Castle Hill", "Bella Vista", "Norwest", "Kellyville",
    "Rouse Hill", "The Ponds", "Stanhope Gardens", "Glenwood", "Parklea",
    "Quakers Hill", "Schofields", "Marsden Park", "Riverstone",
    # North west / Ryde
    "Ryde", "West Ryde", "East Ryde", "North Ryde", "Meadowbank",
    "Denistone", "Eastwood", "Epping", "North Epping", "Carlingford",
    "Beecroft", "Cheltenham", "Pennant Hills", "Thornleigh", "Normanhurst",
    "Hornsby", "Waitara", "Wahroonga", "Warrawee", "Turramurra",
    "Pymble", "Gordon", "Killara", "Lindfield", "Roseville",
    "Chatswood", "Artarmon", "Willoughby", "Naremburn",
    # Lower north shore
    "North Sydney", "McMahons Point", "Lavender Bay", "Milsons Point",
    "Kirribilli", "Neutral Bay", "Cremorne", "Mosman", "Cammeray",
    "Crows Nest", "St Leonards", "Wollstonecraft", "Waverton", "Greenwich",
    "Lane Cove", "Lane Cove North", "Lane Cove West", "Linley Point", "Northwood",
    # Northern beaches
    "Manly", "Fairlight", "Balgowlah", "Seaforth", "Clontarf",
    "Brookvale", "Dee Why", "North Curl Curl", "Curl Curl", "Freshwater",
    "Queenscliff", "Collaroy", "Narrabeen", "Mona Vale", "Bayview",
    "Newport", "Avalon", "Whale Beach", "Palm Beach",
    # Southern Sydney
    "Hurstville", "Penshurst", "Mortdale", "Oatley", "Como",
    "Jannali", "Sutherland", "Kirrawee", "Gymea", "Miranda",
    "Caringbah", "Cronulla", "Woolooware", "Burraneer", "Lilli Pilli",
    "Bangor", "Menai", "Illawong", "Alfords Point", "Padstow",
    "Revesby", "Panania", "Picnic Point", "East Hills",
    "Bankstown", "Yagoona", "Birrong", "Sefton", "Chester Hill",
    # South west
    "Liverpool", "Casula", "Moorebank", "Hammondville", "Holsworthy",
    "Wattle Grove", "Chipping Norton", "Lurnea", "Prestons", "Edmondson Park",
    "Campbelltown", "Glenfield", "Macquarie Fields", "Ingleburn", "Minto",
    "Leumeah", "Bradbury", "Ambarvale", "Rosemeadow",
    # Greater west
    "Penrith", "Kingswood", "Cambridge Park", "Werrington", "St Marys",
    "Mount Druitt", "Rooty Hill", "Doonside", "Blacktown", "Seven Hills",
    "Lalor Park", "Kings Langley", "Glenwood",
    # Regional NSW
    "Newcastle", "Newcastle West", "The Hill", "Cooks Hill", "Bar Beach",
    "Merewether", "Hamilton", "Mayfield", "Charlestown",
    "Wollongong", "North Wollongong", "Fairy Meadow", "Corrimal", "Bulli",
    "Thirroul", "Coledale", "Austinmer", "Stanwell Park",
    "Shellharbour", "Kiama", "Berry", "Nowra", "Ulladulla",
    "Batemans Bay", "Moruya", "Narooma", "Bega", "Eden",
    "Wagga Wagga", "Albury", "Griffith", "Leeton", "Deniliquin",
    "Tamworth", "Armidale", "Inverell", "Glen Innes", "Moree",
    "Dubbo", "Mudgee", "Orange", "Bathurst", "Lithgow",
    "Coffs Harbour", "Port Macquarie", "Taree", "Forster", "Kempsey",
    "Byron Bay", "Lismore", "Ballina", "Tweed Heads", "Murwillumbah",
    "Goulburn", "Bowral", "Mittagong", "Moss Vale",
    "Katoomba", "Leura", "Wentworth Falls", "Springwood", "Glenbrook",
]))
