"""household intent taxonomy: each group has trigger words (what the user says) and a
detection vocabulary (what YOLO-World gets prompted with)."""

from __future__ import annotations

from typing import Dict, List


TAXONOMY: Dict[str, Dict[str, List[str]]] = {

    "drinks_beverages": {
        "triggers": [
            "drink", "drinks", "beverage", "beverages", "soda", "pop", "fizzy",
            "coke", "cola", "sprite", "pepsi", "fanta", "juice", "beer", "wine",
            "alcohol", "booze", "water bottle", "thirsty", "something to drink",
            "energy drink", "soft drink", "soda can", "beer can", "bottle", "bottles",
        ],
        "vocabulary": [
            # sodas / soft drinks
            "coca cola can", "coca cola bottle", "coke can", "diet coke can",
            "pepsi can", "pepsi bottle", "sprite can", "sprite bottle",
            "fanta can", "fanta bottle", "mountain dew can", "dr pepper can",
            "root beer can", "seven up can", "ginger ale can", "cola bottle",
            "soda can", "soda bottle", "two liter soda bottle", "tonic water bottle",
            # energy / sports
            "red bull can", "monster energy can", "rockstar energy can",
            "gatorade bottle", "powerade bottle", "energy drink can",
            "sports drink bottle",
            # water / hot drinks
            "water bottle", "plastic water bottle", "glass water bottle",
            "reusable water bottle", "sparkling water can", "coconut water carton",
            "coffee cup", "paper coffee cup", "travel coffee mug", "tea cup",
            "thermos flask", "insulated tumbler", "hydro flask bottle",
            # juices / dairy drinks
            "juice box", "juice carton", "orange juice carton", "apple juice bottle",
            "milk carton", "milk bottle", "chocolate milk bottle", "smoothie bottle",
            "iced tea bottle", "kombucha bottle", "protein shake bottle",
            # alcohol
            "beer can", "beer bottle", "tall boy beer can", "six pack of beer",
            "wine bottle", "red wine bottle", "white wine bottle", "champagne bottle",
            "liquor bottle", "whiskey bottle", "vodka bottle", "hard seltzer can",
            "cocktail glass", "beer mug", "wine glass",
        ],
    },

    "kitchenware_dining": {
        "triggers": [
            "plate", "plates", "bowl", "bowls", "cup", "cups", "glass", "glasses",
            "mug", "cutlery", "silverware", "utensil", "fork", "knife", "spoon",
            "dish", "dishes", "dishware", "tableware", "pot", "pan", "cookware",
            "set the table", "clear the table", "kitchen", "serving", "chopsticks",
        ],
        "vocabulary": [
            # dishes
            "dinner plate", "side plate", "bowl", "soup bowl", "cereal bowl",
            "salad bowl", "mixing bowl", "serving dish", "platter", "saucer",
            "ramekin", "gravy boat",
            # drinkware
            "drinking glass", "water glass", "wine glass", "champagne flute",
            "cocktail glass", "beer mug", "coffee mug", "tea cup", "espresso cup",
            "tumbler", "shot glass", "pitcher", "jug", "carafe", "teapot",
            # cutlery / utensils
            "fork", "dinner fork", "knife", "butter knife", "steak knife",
            "spoon", "teaspoon", "tablespoon", "chopsticks", "ladle", "spatula",
            "serving spoon", "tongs", "whisk", "peeler", "can opener",
            "bottle opener", "corkscrew", "grater", "rolling pin",
            # cookware
            "cooking pot", "saucepan", "frying pan", "skillet", "wok",
            "stock pot", "dutch oven", "baking tray", "roasting pan",
            "cutting board", "colander", "strainer", "measuring cup",
            "measuring spoon", "kitchen scale", "oven mitt", "trivet",
            "salt shaker", "pepper grinder", "napkin", "placemat", "tablecloth",
        ],
    },

    "small_appliances": {
        "triggers": [
            "appliance", "appliances", "microwave", "toaster", "kettle", "blender",
            "coffee maker", "coffee machine", "air fryer", "mixer", "juicer",
            "rice cooker", "slow cooker", "espresso", "food processor", "countertop",
        ],
        "vocabulary": [
            "microwave oven", "toaster", "toaster oven", "electric kettle",
            "coffee maker", "drip coffee machine", "espresso machine",
            "single serve coffee machine", "blender", "immersion blender",
            "food processor", "stand mixer", "hand mixer", "juicer",
            "air fryer", "rice cooker", "slow cooker", "instant pot pressure cooker",
            "waffle maker", "sandwich press", "electric griddle", "deep fryer",
            "electric grill", "bread machine", "ice cream maker", "popcorn maker",
            "electric can opener", "milk frother", "soda maker",
        ],
    },

    "large_appliances": {
        "triggers": [
            "fridge", "refrigerator", "freezer", "oven", "stove", "range",
            "dishwasher", "washer", "washing machine", "dryer", "large appliance",
            "water heater", "furnace", "hvac",
        ],
        "vocabulary": [
            "refrigerator", "french door refrigerator", "mini fridge",
            "chest freezer", "upright freezer", "oven", "wall oven",
            "gas stove", "electric stove", "cooktop", "kitchen range",
            "range hood", "dishwasher", "washing machine", "front load washer",
            "clothes dryer", "washer dryer combo", "water heater", "furnace",
            "air conditioner unit", "wine cooler", "trash compactor",
        ],
    },

    "audio_video_electronics": {
        "triggers": [
            "speaker", "speakers", "soundbar", "microphone", "mic", "headphones",
            "earbuds", "earphones", "tv", "television", "monitor", "screen",
            "laptop", "computer", "pc", "tablet", "phone", "smartphone", "remote",
            "console", "gaming", "webcam", "keyboard", "mouse", "electronics",
            "audio", "camera", "gadget", "device", "charger", "router",
        ],
        "vocabulary": [
            # audio
            "speaker", "bluetooth speaker", "smart speaker", "bookshelf speaker",
            "soundbar", "subwoofer", "studio monitor speaker", "microphone",
            "usb microphone", "condenser microphone", "wireless microphone",
            "headphones", "over ear headphones", "gaming headset", "earbuds",
            "wireless earbuds", "earphones", "amplifier", "av receiver",
            "record player turntable", "boombox", "portable radio",
            # video / displays
            "television", "flat screen tv", "computer monitor", "projector",
            "projector screen", "digital picture frame",
            # computing
            "laptop", "desktop computer tower", "all in one computer", "tablet",
            "ipad", "smartphone", "mobile phone", "e reader", "smartwatch",
            "computer keyboard", "computer mouse", "trackpad", "webcam",
            "graphics tablet", "usb flash drive", "external hard drive",
            # peripherals / networking / misc
            "remote control", "tv remote", "game controller", "game console",
            "handheld game console", "wifi router", "modem", "network switch",
            "printer", "scanner", "phone charger", "charging cable", "power bank",
            "usb hub", "surge protector", "security camera", "action camera",
            "dslr camera", "drone",
        ],
    },

    "furniture_seating": {
        "triggers": [
            "chair", "chairs", "seat", "seating", "sofa", "couch", "stool",
            "bench", "armchair", "recliner", "ottoman", "sit", "somewhere to sit",
        ],
        "vocabulary": [
            "chair", "dining chair", "office chair", "ergonomic office chair",
            "armchair", "accent chair", "recliner chair", "rocking chair",
            "folding chair", "bar stool", "kitchen stool", "step stool",
            "sofa", "couch", "loveseat", "sectional sofa", "futon", "bench",
            "storage bench", "ottoman", "footstool", "bean bag chair",
            "high chair", "swivel chair", "chaise lounge", "papasan chair",
        ],
    },

    "furniture_surfaces_storage": {
        "triggers": [
            "table", "tables", "desk", "surface", "shelf", "shelves", "bookshelf",
            "cabinet", "cupboard", "drawer", "dresser", "wardrobe", "storage",
            "stand", "counter", "sideboard", "nightstand", "furniture",
        ],
        "vocabulary": [
            "table", "dining table", "coffee table", "side table", "end table",
            "console table", "desk", "computer desk", "standing desk", "writing desk",
            "kitchen counter", "kitchen island", "breakfast bar",
            "bookshelf", "bookcase", "wall shelf", "floating shelf", "shelving unit",
            "cabinet", "kitchen cabinet", "filing cabinet", "display cabinet",
            "cupboard", "pantry cabinet", "chest of drawers", "dresser",
            "nightstand", "wardrobe", "armoire", "closet", "sideboard", "buffet",
            "tv stand", "media console", "shoe rack", "coat rack", "storage cart",
            "storage bin", "storage basket", "cardboard box", "plastic tote",
        ],
    },

    "bedroom_bedding": {
        "triggers": [
            "bed", "beds", "mattress", "pillow", "pillows", "blanket", "blankets",
            "duvet", "comforter", "sheet", "sheets", "bedding", "bedroom", "sleep",
        ],
        "vocabulary": [
            "bed", "single bed", "double bed", "king size bed", "bunk bed",
            "crib", "mattress", "box spring", "headboard", "pillow", "throw pillow",
            "body pillow", "bolster pillow", "blanket", "throw blanket",
            "duvet", "comforter", "quilt", "bedsheet", "fitted sheet", "pillowcase",
            "mattress topper", "electric blanket", "sleeping bag", "cushion",
        ],
    },

    "bathroom_toiletries": {
        "triggers": [
            "bathroom", "toilet", "sink", "bath", "bathtub", "shower", "towel",
            "toothbrush", "toothpaste", "soap", "shampoo", "razor", "toiletries",
            "hygiene", "washroom", "restroom",
        ],
        "vocabulary": [
            "toilet", "toilet paper roll", "toilet brush", "plunger",
            "bathroom sink", "faucet", "bathtub", "shower head", "shower curtain",
            "bath towel", "hand towel", "washcloth", "bath mat", "bathroom scale",
            "toothbrush", "electric toothbrush", "toothpaste tube", "mouthwash bottle",
            "bar of soap", "liquid soap dispenser", "hand sanitizer bottle",
            "shampoo bottle", "conditioner bottle", "body wash bottle",
            "lotion bottle", "deodorant stick", "razor", "electric shaver",
            "shaving cream can", "hairbrush", "comb", "hair dryer", "hair straightener",
            "makeup bag", "cotton swabs", "cotton pads", "tissue box",
            "medicine bottle", "pill bottle", "first aid kit", "bathroom mirror",
        ],
    },

    "cleaning_supplies": {
        "triggers": [
            "clean", "cleaning", "broom", "mop", "vacuum", "hoover", "dustpan",
            "bucket", "sponge", "detergent", "trash", "garbage", "bin", "rubbish",
            "wipe", "tidy", "supplies", "spray",
        ],
        "vocabulary": [
            "broom", "dustpan", "mop", "bucket", "vacuum cleaner",
            "robot vacuum", "handheld vacuum", "sponge", "scrub brush",
            "dish soap bottle", "laundry detergent bottle", "laundry detergent box",
            "fabric softener bottle", "bleach bottle", "all purpose cleaner spray",
            "glass cleaner spray", "disinfectant wipes container", "spray bottle",
            "paper towel roll", "microfiber cloth", "duster", "feather duster",
            "rubber gloves", "trash can", "kitchen trash can", "garbage bag roll",
            "recycling bin", "laundry basket", "clothes hamper", "dustbin",
        ],
    },

    "food_produce_fresh": {
        "triggers": [
            "food", "fruit", "fruits", "vegetable", "veggies", "produce",
            "apple", "banana", "orange", "bread", "eggs", "cheese", "meat",
            "fresh", "groceries", "snack", "hungry", "eat", "something to eat",
        ],
        "vocabulary": [
            # fruit
            "apple", "banana", "orange", "lemon", "lime", "pear", "peach",
            "grapes", "strawberry", "blueberries", "watermelon", "pineapple",
            "mango", "avocado", "kiwi", "cherries",
            # vegetables
            "tomato", "potato", "onion", "garlic", "carrot", "broccoli",
            "lettuce", "cucumber", "bell pepper", "corn", "mushroom", "celery",
            "spinach", "cabbage", "zucchini", "cauliflower",
            # bakery / staples / fresh
            "loaf of bread", "sliced bread", "bagel", "croissant", "baguette",
            "eggs carton", "block of cheese", "stick of butter", "yogurt cup",
            "raw chicken", "steak", "fish fillet", "bacon package", "tofu block",
            # prepared / snack
            "sandwich", "pizza", "hamburger", "hot dog", "salad bowl",
            "bowl of cereal", "donut", "cake", "cookie", "chocolate bar",
        ],
    },

    "pantry_packaged_food": {
        "triggers": [
            "pantry", "packaged", "canned", "cereal", "chips", "snacks",
            "pasta", "rice", "sauce", "jar", "box", "condiment", "condiments",
            "ketchup", "spices", "cupboard food", "dry goods",
        ],
        "vocabulary": [
            "cereal box", "bag of chips", "box of crackers", "package of cookies",
            "canned food tin", "canned soup", "canned beans", "tomato sauce jar",
            "pasta box", "bag of rice", "bag of flour", "sugar bag",
            "ketchup bottle", "mustard bottle", "mayonnaise jar", "hot sauce bottle",
            "soy sauce bottle", "olive oil bottle", "vinegar bottle",
            "peanut butter jar", "jam jar", "honey jar", "nutella jar",
            "salt container", "pepper shaker", "spice jar", "spice rack",
            "tea box", "coffee bag", "instant coffee jar", "protein powder tub",
            "granola bar box", "candy bag", "popcorn bag", "instant noodles cup",
        ],
    },

    "office_stationery": {
        "triggers": [
            "pen", "pencil", "paper", "notebook", "book", "books", "stationery",
            "office", "stapler", "scissors", "tape", "marker", "folder", "desk stuff",
            "calculator", "printer", "write", "notes",
        ],
        "vocabulary": [
            "pen", "ballpoint pen", "pencil", "mechanical pencil", "marker",
            "highlighter", "permanent marker", "crayon", "eraser", "pencil sharpener",
            "notebook", "spiral notebook", "notepad", "sticky notes", "sheet of paper",
            "stack of paper", "book", "hardcover book", "magazine", "newspaper",
            "folder", "file folder", "ring binder", "clipboard", "envelope",
            "stapler", "staple remover", "paper clips", "binder clip", "rubber bands",
            "scissors", "tape dispenser", "roll of tape", "glue stick", "ruler",
            "calculator", "printer", "desk organizer", "pencil case", "whiteboard",
            "whiteboard marker", "push pins", "hole punch",
        ],
    },

    "tools_hardware": {
        "triggers": [
            "tool", "tools", "hardware", "hammer", "screwdriver", "wrench",
            "pliers", "drill", "saw", "toolbox", "nails", "screws", "fix",
            "repair", "workshop", "garage", "measure",
        ],
        "vocabulary": [
            "hammer", "screwdriver", "phillips screwdriver", "flathead screwdriver",
            "wrench", "adjustable wrench", "allen key set", "pliers", "needle nose pliers",
            "wire cutters", "power drill", "cordless drill", "drill bit set",
            "hand saw", "hacksaw", "circular saw", "utility knife", "box cutter",
            "tape measure", "spirit level", "carpenter square", "chisel", "file",
            "sandpaper", "clamp", "vise", "toolbox", "tool bag", "screws box",
            "nails box", "bolts and nuts", "wall anchors", "flashlight",
            "work gloves", "safety glasses", "stud finder", "multimeter",
            "soldering iron", "glue gun", "wd40 can", "paint can", "paint brush",
            "paint roller", "step ladder",
        ],
    },

    "personal_items_wearables": {
        "triggers": [
            "glasses", "sunglasses", "watch", "wallet", "keys", "phone", "bag",
            "backpack", "purse", "handbag", "hat", "cap", "shoes", "jacket",
            "coat", "umbrella", "belt", "clothes", "clothing", "my stuff",
        ],
        "vocabulary": [
            "eyeglasses", "sunglasses", "reading glasses", "wristwatch",
            "smartwatch", "wallet", "purse", "handbag", "keys", "keychain",
            "smartphone", "backpack", "tote bag", "duffel bag", "suitcase",
            "laptop bag", "baseball cap", "beanie hat", "sun hat", "scarf",
            "gloves", "sneakers", "dress shoes", "boots", "sandals", "slippers",
            "jacket", "winter coat", "hoodie", "sweater", "t shirt", "jeans",
            "belt", "tie", "umbrella", "face mask", "lanyard", "jewelry box",
            "necklace", "ring", "bracelet", "earrings",
        ],
    },

    "decor_plants_misc": {
        "triggers": [
            "decor", "decoration", "vase", "plant", "plants", "flower", "flowers",
            "picture", "frame", "painting", "clock", "candle", "mirror", "rug",
            "curtain", "ornament", "art",
        ],
        "vocabulary": [
            "vase", "flower vase", "bouquet of flowers", "potted plant",
            "houseplant", "succulent", "cactus", "hanging plant", "artificial plant",
            "picture frame", "photo frame", "framed painting", "wall art",
            "poster", "tapestry", "wall clock", "desk clock", "alarm clock",
            "candle", "scented candle", "candle holder", "vase of flowers",
            "mirror", "wall mirror", "decorative bowl", "figurine", "sculpture",
            "throw rug", "area rug", "doormat", "curtains", "window blinds",
            "throw pillow", "decorative cushion", "wind chime", "globe",
            "snow globe", "trophy", "aquarium ornament",
        ],
    },

    "lighting": {
        "triggers": [
            "light", "lights", "lamp", "lamps", "bulb", "lighting", "brightness",
            "chandelier", "night light", "dark", "turn on the light",
        ],
        "vocabulary": [
            "table lamp", "desk lamp", "floor lamp", "bedside lamp",
            "ceiling light", "pendant light", "chandelier", "track lighting",
            "recessed light", "wall sconce", "light bulb", "led light bulb",
            "smart bulb", "night light", "string lights", "fairy lights",
            "lava lamp", "flashlight", "lantern", "candle", "reading light",
            "ring light", "neon sign", "led light strip",
        ],
    },

    "safety_power_utility": {
        "triggers": [
            "safety", "fire extinguisher", "smoke detector", "alarm", "thermostat",
            "power", "outlet", "socket", "plug", "extension cord", "battery",
            "charger", "cord", "cable", "surge protector", "electrical",
        ],
        "vocabulary": [
            "fire extinguisher", "smoke detector", "carbon monoxide detector",
            "first aid kit", "fire alarm", "thermostat", "smart thermostat",
            "power outlet", "wall socket", "light switch", "power strip",
            "surge protector", "extension cord", "power adapter", "wall charger",
            "usb charger", "charging cable", "batteries", "aa battery pack",
            "9 volt battery", "power bank", "circuit breaker panel", "flashlight",
            "spare fuse", "door lock", "smart lock", "doorbell camera", "keypad",
        ],
    },

    "laundry": {
        "triggers": [
            "laundry", "iron", "ironing", "hanger", "hangers", "hamper",
            "clothespin", "fold clothes", "wash clothes", "clothesline",
        ],
        "vocabulary": [
            "laundry basket", "clothes hamper", "clothes hanger", "coat hanger",
            "clothing rack", "drying rack", "clothesline", "clothespin",
            "clothes iron", "steam iron", "ironing board", "garment steamer",
            "lint roller", "laundry detergent bottle", "fabric softener bottle",
            "dryer sheets box", "laundry pods container", "stain remover spray",
            "mesh laundry bag",
        ],
    },

    "pets": {
        "triggers": [
            "pet", "pets", "dog", "cat", "animal", "leash", "litter", "aquarium",
            "fish tank", "bird cage", "pet food", "kennel",
        ],
        "vocabulary": [
            "dog", "cat", "pet food bowl", "pet water bowl", "bag of dog food",
            "bag of cat food", "dog leash", "dog collar", "pet harness",
            "cat litter box", "bag of cat litter", "pet bed", "dog crate",
            "cat tree", "scratching post", "pet toy", "chew toy", "aquarium",
            "fish tank", "bird cage", "hamster cage", "pet carrier", "food dispenser",
            "poop bag dispenser", "dog treats bag",
        ],
    },

    "kids_toys": {
        "triggers": [
            "toy", "toys", "kids", "children", "child", "teddy", "doll", "lego",
            "blocks", "board game", "puzzle", "play", "stuffed animal",
        ],
        "vocabulary": [
            "teddy bear", "stuffed animal", "doll", "action figure", "toy car",
            "toy truck", "toy train", "building blocks", "lego set", "board game",
            "jigsaw puzzle", "playing cards", "dice", "rubiks cube", "yo yo",
            "kite", "ball", "soccer ball", "basketball", "beach ball",
            "toy robot", "remote control car", "play kitchen", "toy blocks bin",
            "crayons box", "coloring book", "skateboard", "scooter", "tricycle",
            "rocking horse", "puzzle cube", "marble set",
        ],
    },

    "living_beings": {
        "triggers": [
            "person", "people", "someone", "anyone", "who", "human", "man",
            "woman", "child", "baby", "face", "in the room", "how many people",
        ],
        "vocabulary": [
            "person", "man", "woman", "child", "baby", "toddler",
            "person sitting", "person standing", "person's face", "hand",
            "dog", "cat",
        ],
    },

    "general_common": {
        "triggers": [
            "everything", "anything", "what do you see", "what is this",
            "look around", "describe the room", "general", "stuff", "things",
            "objects", "identify",
        ],
        "vocabulary": [
            "person", "chair", "table", "sofa", "bed", "television", "laptop",
            "smartphone", "remote control", "book", "cup", "bottle", "bowl",
            "plate", "backpack", "handbag", "keys", "wallet", "glasses",
            "clock", "lamp", "potted plant", "picture frame", "pillow", "blanket",
            "speaker", "headphones", "keyboard", "mouse", "coffee mug",
            "water bottle", "trash can", "door", "window", "refrigerator",
            "microwave oven", "toaster", "kettle", "shoes", "hat", "umbrella",
            "toothbrush", "towel", "scissors", "pen", "notebook", "dog", "cat",
        ],
    },
}



def group_names() -> List[str]:
    return list(TAXONOMY.keys())


def vocabulary(group: str) -> List[str]:
    return list(TAXONOMY[group]["vocabulary"])


def triggers(group: str) -> List[str]:
    return list(TAXONOMY[group]["triggers"])


def all_labels() -> List[str]:
    """Every detectable label across every group (with duplicates removed)."""
    seen: Dict[str, None] = {}
    for g in TAXONOMY.values():
        for label in g["vocabulary"]:
            seen.setdefault(label, None)
    return list(seen.keys())


def stats() -> Dict[str, int]:
    """Quick counts for sanity checks / README."""
    return {
        "groups": len(TAXONOMY),
        "total_labels": sum(len(g["vocabulary"]) for g in TAXONOMY.values()),
        "unique_labels": len(all_labels()),
        "total_triggers": sum(len(g["triggers"]) for g in TAXONOMY.values()),
    }


def validate() -> List[str]:
    """Return a list of problems; empty list means the taxonomy is well-formed."""
    problems: List[str] = []
    for name, g in TAXONOMY.items():
        if not name.isidentifier():
            problems.append(f"group name not a safe identifier: {name!r}")
        if "vocabulary" not in g or not g["vocabulary"]:
            problems.append(f"group {name!r} has no vocabulary")
        if "triggers" not in g or not g["triggers"]:
            problems.append(f"group {name!r} has no triggers")
        # duplicate labels within a group
        vocab = g.get("vocabulary", [])
        if len(vocab) != len(set(vocab)):
            dupes = sorted({x for x in vocab if vocab.count(x) > 1})
            problems.append(f"group {name!r} has duplicate labels: {dupes}")
    return problems


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))
    issues = validate()
    print("VALIDATION:", "OK" if not issues else issues)
