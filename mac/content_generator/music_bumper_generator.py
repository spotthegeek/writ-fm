#!/usr/bin/env python3
"""Generate AI music bumpers for WRIT-FM shows using music-gen.server.

Bumpers are 60-120 second instrumental tracks that play between talk segments.
Each show has curated music captions reflecting its vibe and topic focus.

Usage:
    uv run python music_bumper_generator.py --status
    uv run python music_bumper_generator.py --show midnight_signal --count 3
    uv run python music_bumper_generator.py --all --min 5
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

from music_gen_client import MUSIC_GEN_BASE_URL, generate_music, is_server_available

BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"

# Per-show music pools — mix of instrumental and vocal tracks.
# Each entry is either a plain caption string (instrumental) or a dict with
# "caption" and "lyrics" keys (vocal track).
SHOW_MUSIC: dict[str, list[str | dict]] = {
    "midnight_signal": [
        # instrumental — ambient / electronic / minimalist / world
        "Brian Eno ambient, slow evolving organ tones, texture and space",
        "Arvo Pärt tintinnabuli style, sparse piano and violin, sacred minimalism, late night silence",
        "Philip Glass-style piano arpeggio, hypnotic repeating figure, gradually shifting harmonics",
        "late night shortwave radio interference turned into music, static and sine waves, beautiful and eerie",
        "Japanese environmental music, Hiroshi Yoshimura style, water and electronics, peaceful and slow",
        "Sufi devotional music influence, slow circular melody, meditative, ancient feeling",
        "slow chamber music, single cello line, vast reverb, 3am emotional weight, Gorecki inspired",
        "koto and synthesizer fusion, Japanese and electronic, contemplative, floating",
        "Aphex Twin Selected Ambient Works style, warm analog pads, dreamy and weightless",
        "Stars of the Lid style, orchestral drone, enormous slow strings, glacial beauty",
        "Grouper style, washed-out guitar loops, barely there melody, fog and reverb",
        "Harold Budd style, sparse piano notes falling into infinite reverb, crystalline",
        "William Basinski style, decaying tape loops, melancholy and hypnotic, time dissolving",
        "Nils Frahm style, felt piano and synthesizer, intimate and mechanical, warm",
        "Boards of Canada style, nostalgic analog synths, hazy childhood memories, VHS warmth",
        # vocal
        {"caption": "Radiohead style, haunting falsetto, atmospheric guitar, existential loneliness, 3am",
         "lyrics": "[verse]\nThe signal fades and returns again\nAnother night I can't explain\nThe radio hums between the walls\nI hear the frequency that calls\n\n[chorus]\nStill transmitting, still alive\nSomewhere in the static we survive\nThe wavelength bends but never breaks\nI'm listening for listening's sake\n\n[verse]\nThe city sleeps but I remain\nA voice inside the white noise rain\nEach station passed and left behind\nUntil I found this one was mine"},
        {"caption": "Nick Drake style folk, hushed male vocal, acoustic guitar, intimate and melancholic, nighttime",
         "lyrics": "[verse]\nLeave the lamp on by the door\nI've been walking on this floor\nSince the hour turned to blue\nEvery shadow looks like you\n\n[chorus]\nAnd the night is just a room\nWhere the quiet starts to bloom\nI don't mind the dark at all\nIt's the silence when you call\n\n[verse]\nThere's a record on the shelf\nThat I only play myself\nWhen the city holds its breath\nAnd there's nothing left but depth"},
        {"caption": "Bon Iver style, layered ethereal vocals, reverb-soaked, ambient folk, winter atmosphere",
         "lyrics": "[verse]\nThin ice across the morning lake\nEvery step a choice I make\nThe cold cathedral of the pines\nHolds the memory between the lines\n\n[chorus]\nI am the echo, not the sound\nStill reaching for the frozen ground\nThe signal carries through the snow\nTo someone I used to know"},
        {"caption": "Elliott Smith style, gentle double-tracked vocal, fingerpicked guitar, whispered confession, late night",
         "lyrics": "[verse]\nBetween the hours and the walls\nThe telephone that never calls\nI'm tuning in to something low\nA frequency the neighbors don't know\n\n[chorus]\nKeep the volume down tonight\nSome things only work in half-light\nThe words come easier this way\nWhen there's nothing left to say\n\n[verse]\nThe ambulance goes past the street\nThe radiator's ticking heat\nAnother night of lying still\nAnother frequency to fill"},
        {"caption": "Sigur Rós style, soaring falsetto, bowed guitar, Icelandic grandeur, emotional and vast",
         "lyrics": "[verse]\nThe glacier moves an inch a year\nSo slow you'd never know it's here\nBut everything it touches changes shape\nThe mountain and the lake\n\n[chorus]\nWe are the slow things\nWe are the deep time\nMoving beneath the surface of the world\nThe ancient signal unfurled\n\n[verse]\nA lighthouse turning in the dark\nEach revolution leaves a mark\nOn water that forgets immediately\nThe way that light moves endlessly"},
        {"caption": "Leonard Cohen style, deep baritone spoken-sung, sparse piano, philosophical, late night wisdom",
         "lyrics": "[verse]\nThere's a crack in the frequency tonight\nA place where the darkness lets in light\nI've been sitting with this microphone\nTalking to the beautifully alone\n\n[chorus]\nThe signal doesn't promise much\nJust a voice, just a touch\nOf something real in the machine\nThe truest thing I've ever seen\n\n[verse]\nSo here's to the ones who can't sleep\nWhose thoughts run too wide and too deep\nThe radio was made for you\nA stranger telling something true"},
    ],
    "the_night_garden": [
        # instrumental — gothic / dark / world / classical darkness
        "Dead Can Dance inspired, dark medieval atmosphere, ceremonial drums, ancient and mysterious",
        "Bulgarian folk music influence, strange intervals, modal harmonics, forest at night",
        "gamelan orchestra, slow and ceremonial, Indonesian bronze percussion, twilight ritual",
        "drone metal without distortion, sustained notes, enormous and slow, cathedral reverb",
        "witch house electronic, slow haunted beats, distorted harp, cursed and beautiful",
        "Tuvan throat singing overtones, harmonic resonances, hypnotic, ritual",
        "Shostakovich string quartet energy, tense and melancholic, Soviet-era emotional weight",
        "harpsichord with reverb, Baroque but wrong, slightly detuned, dreamlike",
        "dark cabaret waltz, Tom Waits piano style, smoky and off-kilter, broken music box",
        "Hildegard von Bingen style sacred chant, monophonic, ancient stone chapel reverb",
        "Akira Yamaoka style dark ambient, industrial textures, fog and rust, psychological",
        "Lustmord style dark ambient, subterranean drones, cave reverb, geological time",
        "midnight harp solo, Celtic mourning, frost and starlight, ancient grief",
        "prepared piano, John Cage influence, objects on strings, beautiful wrongness",
        # vocal
        {"caption": "Cocteau Twins style, dreamy female vocal, ethereal shoegaze, lush reverb, nocturnal",
         "lyrics": "[verse]\nMoonflower opening in the rain\nSilver threads of sweet refrain\nThe garden speaks in tongues of dew\nEvery petal turning blue\n\n[chorus]\nBloom in darkness, bloom in dreams\nNothing ever is what it seems\nThe night has petals of its own\nSeeds of light the stars have sown\n\n[verse]\nWalking barefoot on the moss\nEvery step a gain, a loss\nThe owls are singing vespers now\nThe moon hangs low on every bough"},
        {"caption": "Björk style, dramatic female vocal, electronic textures, dark and beautiful, ritualistic",
         "lyrics": "[verse]\nUnderneath the bark and bone\nThere's a frequency unknown\nI can feel it in my teeth\nIn the earth and underneath\n\n[chorus]\nLet the darkness be the door\nI have been here once before\nEvery ending starts again\nGrowth is just another name for pain\n\n[bridge]\nThe roots go deeper than you think\nRight down to the very brink\nWhere the oldest water flows\nAnd the night garden grows"},
        {"caption": "Mazzy Star style, drowsy female vocal, slide guitar, dark dreampop, hypnotic and slow",
         "lyrics": "[verse]\nFade into the velvet hour\nWatch the darkness eat the flower\nI was waiting by the well\nFor a story I can't tell\n\n[chorus]\nClose your eyes and follow me\nThrough the dark between the trees\nWe don't need to understand\nJust the feel of night on skin"},
        {"caption": "Chelsea Wolfe style, dark folk, powerful female vocal, cello and distortion, ominous beauty",
         "lyrics": "[verse]\nThe iron gate is rusted shut\nThe garden grows from root to gut\nAll the flowers here have teeth\nAll the beauty grows beneath\n\n[chorus]\nLet me in, let me in\nTo the place where dark begins\nI'm not afraid of what I'll find\nI left the daylight far behind\n\n[verse]\nThe moths are drawn to every light\nBut I prefer the edge of night\nWhere shadows have more shape than things\nAnd silence is the song that sings"},
        {"caption": "Siouxsie Sioux style, dramatic female vocal, post-punk goth, angular guitar, nocturnal power",
         "lyrics": "[verse]\nThe spiders weave in silver thread\nA message for the living dead\nThe night is not a thing to fear\nThe night is why we gather here\n\n[chorus]\nIn the garden after dark\nEvery creature leaves a mark\nThe predator, the prey, the bloom\nAll of us inside this room"},
    ],
    "dawn_chorus": [
        # instrumental — jazz / world / folk / classical
        "ECM Records morning jazz, Keith Jarrett solo piano feeling, spacious and lyrical, sunrise",
        "Brazilian choro, acoustic guitar and flute, quick and joyful, Rio morning",
        "West African kora solo, intricate fingerpicked patterns, warm and golden, morning light",
        "Debussy impressionist piano, shimmering and light, morning mist on water",
        "cool jazz trio, West Coast 1950s feel, relaxed brushed drums, morning coffee",
        "Appalachian folk, fingerpicked guitar and fiddle, dew on grass, American morning",
        "Hawaiian slack key guitar, gentle and open, Pacific morning, ocean breeze",
        "Nordic folk melody, solo flute, cold clean air, Scandinavian dawn",
        "Vince Guaraldi style jazz piano, playful and warm, Sunday morning cartoon nostalgia",
        "Pat Metheny style jazz guitar, warm tone, spacious Americana, golden hour",
        "Antonio Carlos Jobim bossa nova, gentle guitar and piano, Rio de Janeiro sunrise",
        "Penguin Cafe Orchestra style, chamber folk, quirky and warm, English morning",
        "solo classical guitar, Segovia style, Spanish morning, courtyard and fountain",
        "steel pan solo, Caribbean morning, bright and joyful, island awakening",
        # vocal
        {"caption": "Van Morrison style, warm soulful vocal, acoustic guitar, morning glory, gentle and earthy",
         "lyrics": "[verse]\nThe coffee's on, the windows wide\nThe morning comes with nothing to hide\nA sparrow lands on the fire escape\nThe whole world waking at its own pace\n\n[chorus]\nAnother morning, another chance\nTo watch the light begin its dance\nAcross the floor and up the wall\nThe simplest miracle of all\n\n[verse]\nThe baker's bread across the street\nThe sound of early morning feet\nSomewhere a radio starts to play\nThe first good song of a brand new day"},
        {"caption": "Norah Jones style, intimate female vocal, piano and brushed drums, warm morning jazz, gentle",
         "lyrics": "[verse]\nSunrise on the kitchen tiles\nI've been up for quite a while\nWatching shadows lose their hold\nAs the morning turns to gold\n\n[chorus]\nPour another cup for me\nLet the morning set us free\nNo appointments, no demands\nJust the warmth of open hands\n\n[verse]\nThe newspaper can wait today\nI'd rather watch the curtains sway\nAnd listen to the world outside\nSlowly waking, satisfied"},
        {"caption": "Iron and Wine style, whispery male folk vocal, fingerpicked guitar, tender and pastoral",
         "lyrics": "[verse]\nDew on the clover, light on the hill\nThe morning is patient and perfectly still\nA cardinal lands on the telephone wire\nThe whole world tuning to something higher\n\n[chorus]\nGood morning to the ones who stayed\nWho made it through the night afraid\nThe sun is here, the dark is done\nWe start again, we start as one"},
        {"caption": "Feist style, clear female vocal, indie folk pop, handclaps and acoustic guitar, optimistic dawn",
         "lyrics": "[verse]\nOne two three four\nDaylight coming through the door\nFive six seven eight\nThe morning doesn't make you wait\n\n[chorus]\nEvery sunrise is a song\nThat the earth has known so long\nWe just borrow it a while\nWalk a little, share a smile\n\n[verse]\nBirds outside the window frame\nNot a single one the same\nEach one singing their own part\nA chorus from the very start"},
        {"caption": "Jack Johnson style, laid-back male vocal, acoustic guitar, breezy morning, surf and sunshine",
         "lyrics": "[verse]\nBanana pancakes on the stove\nThe morning moves the way it goes\nNo shoes, no hurry, no alarm\nJust the sun on my right arm\n\n[chorus]\nLet the morning take its time\nEvery moment feels like rhyme\nThe ocean's just a bike ride out\nThat's what mornings are about\n\n[verse]\nThe neighbor's dog is at the fence\nThe whole block's waking up from hence\nSprinklers making little rainbows\nEverything the sunrise knows"},
    ],
    "sonic_archaeology": [
        # instrumental — jazz / funk / world / deep cuts
        "Ethiopian jazz, Mulatu Astatke style, unusual scales, Addis Ababa 1970s, brass and vibraphone",
        "Jamaican ska, 1960s Kingston, choppy organ, walking bass, pre-reggae energy",
        "cumbia, Colombian coast, accordion and caja drum, dusty and joyful",
        "Canterbury prog rock, Soft Machine influence, jazz-rock fusion, 1971 underground",
        "library music, 1970s BBC Sound Effects style, weird electronic stingers, forgotten genre",
        "French ye-ye, 1960s Paris, twangy guitars, Gainsbourg session musicians",
        "Chicago blues, Muddy Waters era, slide guitar and harmonica, Chess Records feel",
        "Afrobeat, Fela Kuti influence, interlocking rhythms, Lagos 1970s",
        "Turkish psychedelic folk, saz and fuzz guitar, 1970s Anatolian rock",
        "Tropicália, Os Mutantes style, Brazilian psych pop, chaotic and beautiful, 1968",
        "krautrock motorik beat, Neu! style, hypnotic repetition, Düsseldorf 1972",
        "Saharan desert blues, Tinariwen style, electric guitar and hand drums, vast and dusty",
        "Bollywood soundtrack, 1970s RD Burman style, sitar meets disco, Bombay funk",
        "Peruvian chicha, cumbia and surf guitar, Lima 1970s, tropical psychedelia",
        "Soviet-era jazz, Ganelin Trio style, free improv behind the Iron Curtain",
        # vocal
        {"caption": "Fela Kuti style Afrobeat, call and response vocals, Lagos 1970s, brass and polyrhythm, political",
         "lyrics": "[verse]\nThey build the walls but we build the sound\nThe rhythm rises from the ground\nEvery city has a beat\nBorn from struggle, born from heat\n\n[chorus]\nDig it up, dig it up, dig it up now\nThe music buried underground\nDig it up, dig it up, dig it up now\nThe sound they tried to keep us down\n\n[verse]\nFrom Kingston town to Addis beat\nThe record spins beneath our feet\nA saxophone from '73\nStill playing for the yet to be"},
        {"caption": "Serge Gainsbourg style French pop, breathy vocal, orchestral arrangement, 1960s Paris sophistication",
         "lyrics": "[verse]\nCigarette smoke and cellulose\nThe projector hums, the curtain goes\nA melody from Avenue B\nIs playing on the Champs-Élysées\n\n[chorus]\nThe record skips but still it plays\nAcross the decades, through the haze\nSome songs refuse to stay in time\nThey cross the border, cross the line\n\n[verse]\nA studio in Muscle Shoals\nConnected to a thousand souls\nWho pressed the wax and passed it on\nThe archaeology of song"},
        {"caption": "Lee Scratch Perry style dub reggae, deep bass, echo chamber vocals, analog warmth, mystic",
         "lyrics": "[verse]\nThrough the echo chamber deep\nWhere the bassline goes to sleep\nAnd the reverb tells a tale\nOf a ship without a sail\n\n[chorus]\nDub it down to bone and wire\nStrip the sound to holy fire\nWhat remains when all is gone\nIs the rhythm carrying on"},
        {"caption": "Caetano Veloso style tropicália, Portuguese vocal, acoustic guitar and strings, Brazilian poetry",
         "lyrics": "[verse]\nThe record store on Rua Augusta\nHas everything you never lost-a\nA pressing from Bahia, 1969\nThe grooves still warm like summer wine\n\n[chorus]\nEvery record is a door\nTo a room that's been before\nStep inside and hear the ghosts\nOf the songs that matter most\n\n[verse]\nThe stylus drops, the crackle starts\nThe sound of someone's beating heart\nPreserved in vinyl, kept in wax\nThe past that nothing can relax"},
        {"caption": "Ali Farka Touré style desert blues, West African vocal, ngoni and guitar, ancient and modern",
         "lyrics": "[verse]\nThe river bends at Niafunké\nThe music older than the clay\nA string plucked underneath the stars\nConnects the earth to who we are\n\n[chorus]\nThe blues began before the name\nAcross the water, still the same\nA melody the desert knows\nThat everywhere the river flows"},
    ],
    "signal_report": [
        # instrumental — post-punk / electronic / tension / cerebral
        "industrial techno, Berlin late 90s, sparse kick drum, cold machinery, Surgeon influence",
        "Detroit techno minimal, Robert Hood style, stripped to essentials, hypnotic and urgent",
        "musique concrète, found sounds assembled into rhythm, unsettling and cerebral",
        "free jazz, Ornette Coleman energy, no fixed rhythm, searching and urgent",
        "Gang of Four inspired, dry funk guitar riff, angular and political, post-punk",
        "North African gnawa ritual rhythm, trance percussion, ancient trance induction",
        "gamelan percussion with glitch electronics, Indonesian tradition meets digital, tense",
        "reggaeton dembow beat stripped to bones, just the rhythm pattern, hypnotic loop",
        "Autechre style IDM, broken beats, alien rhythms, digital abstraction",
        "Burial style UK garage, vinyl crackle, 2-step rhythms, rainy London midnight",
        "Battles style math rock, interlocking guitar patterns, precise and anxious",
        "Amon Tobin style, cinematic breakbeat, chopped samples, urban tension",
        "Squarepusher style drum and bass, frenetic broken beats, jazz fusion gone digital",
        "Demdike Stare style, dark electronic, industrial textures, hauntological dread",
        # vocal
        {"caption": "Talking Heads style, nervous new wave vocal, angular guitar, urban paranoia, post-punk",
         "lyrics": "[verse]\nThe headlines scroll across the screen\nWhat do they say and what do they mean\nThe anchor smiles but something's wrong\nThe broadcast cuts, the signal's gone\n\n[chorus]\nWhat's the frequency\nWho's controlling me\nEvery channel tells a different truth\nNone of them have any proof\n\n[verse]\nI read the paper front to back\nThe words are white, the ink is black\nBetween the lines there's empty space\nWhere all the real news leaves no trace"},
        {"caption": "Massive Attack style trip-hop, deep male vocal, dark electronic, Bristol sound, ominous",
         "lyrics": "[verse]\nProtection from the evening news\nThe signal that we cannot use\nA city made of ones and zeros\nManufacturing its heroes\n\n[chorus]\nReport the signal, not the noise\nThe frequency beneath the poise\nSomething's moving underground\nYou can feel it in the sound\n\n[verse]\nThe satellite looks down at night\nOn every window, every light\nEach one a story, each one a feed\nEach one a mouth that needs to read"},
        {"caption": "Portishead style, melancholic female vocal, scratchy vinyl samples, noir atmosphere, trip-hop",
         "lyrics": "[verse]\nStatic on the midnight wire\nThe truth is drowning in the fire\nI tune the dial but nothing's clear\nJust the ghost of last year's fear\n\n[chorus]\nGive me signal, cut the noise\nGive me something, not a choice\nBetween two lies dressed up as fact\nI want the story they redact"},
        {"caption": "TV on the Radio style, layered male vocals, art rock, political urgency, textural",
         "lyrics": "[verse]\nThe algorithm knows your name\nIt feeds you fury, feeds you blame\nThe screen is just a window frame\nAround a very careful game\n\n[chorus]\nWho reports the reporters\nWho patrols the borders\nBetween what's true and what is sold\nThe signal's worth its weight in gold\n\n[verse]\nA journalist in a parking lot\nRecording what the cameras caught\nThe story that the suits won't run\nThe war that no one says they've won"},
        {"caption": "Thom Yorke solo style, glitchy electronic, anxious falsetto, paranoid beauty, digital age",
         "lyrics": "[verse]\nThe data streams like weather now\nIt rains on everyone somehow\nYour face is in a thousand files\nYour habits tracked across the miles\n\n[chorus]\nAre you listening\nIs anyone listening\nThe frequency is set so low\nOnly the awake would know\n\n[verse]\nI wrote this on a borrowed phone\nIn a language not my own\nThe signal bounced from tower to tower\nUntil it reached the midnight hour"},
    ],
    "the_groove_lab": [
        # instrumental — soul / funk / groove / world
        "New Orleans second line brass band, Mardi Gras energy, tuba and snare, jubilant",
        "Chicago stepping music, smooth house soul, 120 bpm, elegant, Saturday night",
        "Kendrick Lamar era jazz-rap beat, Flying Lotus influence, off-kilter groove, West Coast",
        "Memphis soul, Hi Records style, Al Green session band, greasy and warm",
        "Afrobeat groove, Fela Kuti style, Lagos brass section, polyrhythmic and unstoppable",
        "Brazilian baile funk, Rio favela bass, heavy 808, polyrhythmic",
        "Caribbean dancehall riddim, Kingston Jamaica, digital reggae, stepping bass",
        "Southern gospel, Hammond B3 organ, testifying energy, church groove",
        "Parliament Funkadelic style, heavy funk, Bootsy bass, mothership groove",
        "Herbie Hancock Headhunters style, jazz funk, clavinet and ARP synth, 1973",
        "J Dilla style boom bap, wonky drums, vinyl samples, Detroit hip-hop soul",
        "Cymande style funk, British Afro-funk, warm and deep, underground groove, 1972",
        "Roy Ayers vibraphone funk, smooth and warm, summer in the city groove",
        "Khruangbin style, psychedelic soul, Thai funk influence, minimal and hypnotic",
        # vocal
        {"caption": "Curtis Mayfield style soul, falsetto vocal, wah guitar, lush strings, conscious groove",
         "lyrics": "[verse]\nThe bassline walks you through the door\nThe kind of room you're looking for\nWhere everybody knows the feel\nAnd everything you touch is real\n\n[chorus]\nMove your body, free your mind\nLeave the weight of the world behind\nThe groove don't judge, the groove don't lie\nThe groove is the reason, the groove is the why\n\n[verse]\nA Hammond organ starts to preach\nThe sermon that the words can't reach\nThe drummer's in a state of grace\nSweat and scripture on their face"},
        {"caption": "D'Angelo style neo-soul, silky male vocal, Rhodes piano, laid-back hip-hop beat, midnight groove",
         "lyrics": "[verse]\nSlowed it down to half the speed\nGave the bass what the bass would need\nLet the Rhodes just ring and ring\nUntil the whole room starts to sing\n\n[chorus]\nThis is the groove lab, come inside\nCheck your ego, check your pride\nNothing here but feel and flow\nLet the music let you go\n\n[verse]\nThe hi-hat whispers, snare replies\nThe conversation never dies\nBetween the kick and the bassline deep\nThere's a rhythm you can keep"},
        {"caption": "Erykah Badu style neo-soul, warm female vocal, analog synths, spiritual groove, Afrofuturist",
         "lyrics": "[verse]\nIncense and the turntable spin\nThe ancestors are coming in\nThrough the speaker through the wire\nSetting every cell on fire\n\n[chorus]\nFeel it in your bones tonight\nThe frequency is set to right\nThe groove was here before the word\nThe oldest prayer ever heard\n\n[verse]\nVinyl crackle, candle flame\nEvery session feels the same\nLike coming home to holy ground\nWhere the lost get found through sound"},
        {"caption": "Stevie Wonder style, joyful vocal, clavinet and horns, funk-soul sunshine, 1970s genius",
         "lyrics": "[verse]\nWoke up this morning with a song\nBeen trying to sing it all day long\nThe melody won't let me be\nIt's got a hold, it's got the key\n\n[chorus]\nSuperstition or the groove\nEither way it makes you move\nThe rhythm's got you by the heart\nThat's the science, that's the art\n\n[verse]\nEvery instrument alive\nThe horns are shouting, drums arrive\nThe bass is walking down the street\nInviting everyone to meet"},
        {"caption": "Marvin Gaye style smooth soul, tender male vocal, strings and congas, sensual and political",
         "lyrics": "[verse]\nWhat's going on across the wire\nThe world is set to catch on fire\nBut here inside the groove we're safe\nThe music is a sacred space\n\n[chorus]\nLet the bassline heal the wound\nLet the saxophone commune\nWith the part of us that knows\nThe groove is where the real love grows\n\n[verse]\nMercy mercy me the sound\nOf something real and something found\nBetween the heartbreak and the beat\nWhere justice and the rhythm meet"},
    ],
    "crosswire": [
        # instrumental — collisions / experimental / global fusion
        "Balkan brass band meets electronic production, chaotic and joyful, wedding energy",
        "John Zorn Naked City energy, genre-jumping every 30 seconds, jazz noise rock",
        "Afropop meets indie rock, chimurenga guitar riffs with reverb, Zimbabwe meets Brooklyn",
        "Turkish psychedelic rock, Anatolian folk with fuzz guitar, 1970s Istanbul underground",
        "gnawa meets jazz fusion, Moroccan rhythms with improvised saxophone, cross-cultural",
        "Steve Reich-style phasing, two instruments slightly out of sync creating new rhythms",
        "free improvisation, two instruments having an argument, atonal then resolution",
        "neo-soul meets flamenco, Rhodes piano with palmas clapping, unexpected conversation",
        "Ennio Morricone meets Massive Attack, spaghetti western trip-hop, epic tension",
        "Indian classical raga meets electronic, sitar and 808, ancient meets future",
        "klezmer meets drum and bass, clarinet over breakbeats, absurd and brilliant",
        "Japanese noise rock meets bossa nova, Boredoms meets Jobim, impossible beauty",
        "Celtic fiddle meets Afrobeat drums, Irish meets Nigerian, green meets gold",
        "mariachi horns meet shoegaze guitar, Mexico meets Manchester, enormous and romantic",
        # vocal
        {"caption": "The Clash style punk rock meets reggae, urgent vocals, political, genre collision, rebel music",
         "lyrics": "[verse]\nThe border's just a line they drew\nBut music doesn't need a queue\nIt crosses over, breaks the wall\nThe rhythm doesn't care at all\n\n[chorus]\nCrosswire, crossfire, cross the line\nEvery genre's intertwined\nYou can't contain it, can't define\nThe sound of two worlds in a bind\n\n[verse]\nA sitar strings meet a 808\nA tabla drum and a drum machine debate\nWho got here first doesn't matter now\nThe fusion's here, take a bow"},
        {"caption": "MIA style global electronic pop, female vocal, dancehall meets digital, polyglot energy, chaotic",
         "lyrics": "[verse]\nLagos to London on a beat\nBangkok bass beneath your feet\nEvery border got a sound\nThat the passport hasn't found\n\n[chorus]\nWires crossed and tangled up\nOverflowing from the cup\nTwo ideas having a fight\nTurning friction into light\n\n[verse]\nKumasi highlife, Berlin dub\nMeeting in a basement club\nNeither wins and neither loses\nThe collision's what the muse is"},
        {"caption": "Gogol Bordello style gypsy punk, manic energy, accordion and electric guitar, immigrant celebration",
         "lyrics": "[verse]\nBring the fiddle, bring the noise\nBring the unacceptable joys\nOf people who refuse to fit\nInto the box they built for it\n\n[chorus]\nWe are the crosswire, we are the spark\nWhere two traditions meet in the dark\nNeither pure and neither clean\nThe most alive thing you've ever seen"},
        {"caption": "Calle 13 style, Spanish rap over global rhythms, revolutionary, genre-defying Latin alternative",
         "lyrics": "[verse]\nThe accordion from Bogotá\nMeets the beatbox from Panama\nA fiddle from the Appalachian hills\nPlaying over Bristol bass that kills\n\n[chorus]\nNo genre is an island here\nThe crosswire makes it crystal clear\nThat music doesn't need a flag\nIt moves in every language's drag\n\n[verse]\nThe purists hate it, that's the proof\nThat something's happening on the roof\nWhere all the sounds they kept apart\nAre finally sharing one big heart"},
        {"caption": "System of a Down meets Armenian folk, powerful male vocal, heavy and melodic, cultural fury",
         "lyrics": "[verse]\nThe duduk cries over power chords\nThe ancient and the modern swords\nCross in the air above the pit\nWhere tradition meets the new and it\n\n[chorus]\nExplodes into a thousand pieces\nEvery fragment finds its thesis\nIn the space between two truths\nLives the wildest kind of youth"},
    ],
    "listener_hours": [
        # instrumental — intimate / communal / warm
        "New Orleans jazz funeral slow march, solemn then joyful, community grief and celebration",
        "Irish session music, fiddle and bodhran, pub warmth, everyone welcome",
        "bossa nova guitar solo, intimate Ipanema apartment, late Sunday, gentle and personal",
        "Malian kora and ngoni duet, griot storytelling tradition, West African warmth",
        "Congolese rumba, soukous guitar, warm and communal, African living room",
        "sea shanty rhythm, work song energy, collective effort, pulling together",
        "Andean folk, pan flute and charango, mountain village, ancient and communal",
        "Hawaiian ukulele and slack key, island warmth, porch at sunset",
        "Django Reinhardt style gypsy jazz, acoustic guitar, campfire energy, social and warm",
        "Appalachian front porch bluegrass, banjo and mandolin, neighborly and earthy",
        "Greek rebetiko, bouzouki and guitar, taverna warmth, shared wine and stories",
        "Cuban son, tres guitar and congas, Havana living room, intimate dance",
        "Cape Verdean morna, acoustic guitar, melancholic warmth, Cesária Évora style",
        "ragtime piano, honky-tonk warmth, 1920s parlor, communal joy",
        # vocal
        {"caption": "Bill Withers style soul, simple and honest vocal, acoustic guitar, human warmth, communal",
         "lyrics": "[verse]\nI got your letter on a Tuesday night\nYou said the station helped you through the fight\nBetween the person that you are\nAnd the one you see so far away\n\n[chorus]\nWe're all just voices in the dark\nLooking for a place to park\nOur heavy hearts and tired bones\nYou're not listening alone\n\n[verse]\nSomeone in Detroit stays up late\nSomeone in Dublin can relate\nWe never see each other's face\nBut we share this little space"},
        {"caption": "Tracy Chapman style folk soul, intimate female vocal, acoustic guitar, storytelling, honest",
         "lyrics": "[verse]\nYou wrote to say you drive at night\nThe highway empty, dashboard light\nThis station is the only sound\nThat keeps your feet upon the ground\n\n[chorus]\nThank you for the frequency\nFor finding us, for finding me\nThe mailbag opens every week\nFor everyone who needs to speak\n\n[verse]\nA teacher writes from Tennessee\nA night nurse from the Salton Sea\nEach letter is a little proof\nThat someone's underneath this roof"},
        {"caption": "Cat Stevens style folk, warm male vocal, acoustic guitar, gentle wisdom, timeless",
         "lyrics": "[verse]\nThe messages come one by one\nFrom places underneath the sun\nAnd underneath the moon as well\nEach one a story, each one a spell\n\n[chorus]\nSo keep on writing, keep on through\nThe signal carries me to you\nAcross the static, through the night\nYour words become the broadcast light"},
        {"caption": "Nina Simone style, powerful female vocal, sparse piano, raw emotion, intimate and commanding",
         "lyrics": "[verse]\nSomeone wrote to say they cried\nListening in the car outside\nTheir house because they couldn't face\nThe silence of an empty space\n\n[chorus]\nI hear you, I hear you\nThrough the wire, through the blue\nThe music is the bridge we build\nBetween the broken and the healed\n\n[verse]\nAnother says the show last week\nSaid the thing they couldn't speak\nSo here's to voices in the night\nWho make the darkness feel like light"},
        {"caption": "James Taylor style, gentle male vocal, acoustic guitar, comforting and wise, fireside warmth",
         "lyrics": "[verse]\nA letter from a midnight town\nSays the signal never lets them down\nWhen the world gets loud and mean\nThis frequency keeps them clean\n\n[chorus]\nYou've got a friend inside the wire\nA voice beside the dying fire\nWhenever you need somewhere to be\nJust tune in to this frequency\n\n[verse]\nThe mailbag's full again tonight\nWith people reaching for the light\nEach word a hand held in the dark\nEach message is a tiny spark"},
    ],
}

# Duration range for bumpers (seconds)
BUMPER_MIN = 180.0
BUMPER_MAX = 300.0


def _display_name(caption: str) -> str:
    """Extract a short display-friendly name from the caption (first 2-3 words)."""
    first_part = caption.split(",")[0].strip()
    words = first_part.split()
    return " ".join(words[:3]).title()


def bumper_count(show_id: str) -> int:
    """Count pre-generated bumpers for a show."""
    show_dir = BUMPERS_DIR / show_id
    if not show_dir.exists():
        return 0
    return sum(1 for f in show_dir.iterdir() if f.suffix.lower() in {".flac", ".mp3", ".wav"})


def print_status():
    """Print bumper count per show."""
    print("AI Music Bumper Status:")
    print("-" * 40)
    total = 0
    for show_id in SHOW_MUSIC:
        count = bumper_count(show_id)
        total += count
        status = "OK" if count >= 5 else ("LOW" if count > 0 else "EMPTY")
        print(f"  {show_id:<25} {count:3d}  [{status}]")
    print(f"\n  Total: {total}")


def generate_one_bumper(show_id: str, verbose: bool = True) -> bool:
    """Generate one AI music track for a show. Returns True on success."""
    if show_id not in SHOW_MUSIC:
        print(f"Unknown show: {show_id}")
        return False

    entry = random.choice(SHOW_MUSIC[show_id])

    # Entry is either a plain caption string (instrumental) or a dict with lyrics
    if isinstance(entry, dict):
        caption = entry["caption"]
        lyrics = entry["lyrics"]
        instrumental = False
    else:
        caption = entry
        lyrics = "[Instrumental]"
        instrumental = True

    duration = round(random.uniform(BUMPER_MIN, BUMPER_MAX), 1)

    show_dir = BUMPERS_DIR / show_id
    show_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_path = show_dir / f"{show_id}_bumper_{timestamp}.flac"
    meta_path = audio_path.with_suffix(".json")

    kind = "vocal" if not instrumental else "instrumental"
    if verbose:
        print(f"  [{show_id}] {int(duration)}s ({kind}) — {caption[:70]}...")

    start = time.perf_counter()
    ok = generate_music(caption, audio_path, duration=duration,
                        instrumental=instrumental, lyrics=lyrics)
    elapsed = time.perf_counter() - start

    if ok:
        meta = {
            "show_id": show_id,
            "caption": caption,
            "display_name": _display_name(caption),
            "duration": duration,
            "instrumental": instrumental,
            "generated_at": datetime.now().isoformat(),
            "generation_seconds": round(elapsed, 1),
            "ai_generated": True,
            "model": "ace-step",
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        if verbose:
            print(f"  Saved: {audio_path.name} ({elapsed:.0f}s)")
        return True
    else:
        if verbose:
            print(f"  FAILED for {show_id}")
        return False


def generate_bumpers_for_show(show_id: str, count: int = 3, verbose: bool = True) -> int:
    """Generate `count` AI bumpers for a show. Returns number successfully generated."""
    generated = 0
    for i in range(count):
        if verbose:
            print(f"[{i+1}/{count}] Generating...")
        if generate_one_bumper(show_id, verbose=verbose):
            generated += 1
    return generated


def main():
    parser = argparse.ArgumentParser(description="Generate AI music bumpers for WRIT-FM")
    parser.add_argument("--show", help="Show ID to generate for")
    parser.add_argument("--all", action="store_true", help="Generate for all shows")
    parser.add_argument("--count", type=int, default=3, help="Bumpers to generate per show")
    parser.add_argument("--status", action="store_true", help="Show bumper counts and exit")
    parser.add_argument("--min", type=int, default=5, help="Min bumpers threshold (used with --all)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if not is_server_available():
        print(f"music-gen.server not available at {MUSIC_GEN_BASE_URL}")
        print("Start it with:")
        print("  cd /path/to/music-gen.server && uv run uvicorn src.kortexa.music_gen.server:app --port 4009")
        sys.exit(1)

    if args.all:
        for show_id in SHOW_MUSIC:
            current = bumper_count(show_id)
            if current >= args.min:
                print(f"  {show_id}: {current} bumpers (OK)")
                continue
            needed = args.min - current
            print(f"\nGenerating {needed} bumpers for {show_id} (have {current})...")
            generate_bumpers_for_show(show_id, count=needed)
        return

    if args.show:
        if args.show not in SHOW_MUSIC:
            print(f"Unknown show '{args.show}'. Valid shows: {', '.join(SHOW_MUSIC)}")
            sys.exit(1)
        total = generate_bumpers_for_show(args.show, count=args.count)
        print(f"\nGenerated {total}/{args.count} bumpers for {args.show}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
