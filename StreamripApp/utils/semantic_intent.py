import gzip
import base64
import math
from typing import Optional, Tuple

from utils.bge_vocab_data import EMBEDDED_VOCAB

def levenshtein_distance(s1: str, s2: str) -> int:
    """Computes the Levenshtein distance between two strings in pure Python."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
        
    return previous_row[-1]


class WordPieceTokenizer:
    def __init__(self):
        self.vocab = {}
        # Decode and decompress the embedded vocabulary
        vocab_bytes = gzip.decompress(base64.b64decode(EMBEDDED_VOCAB))
        vocab_str = vocab_bytes.decode("utf-8")
        for i, line in enumerate(vocab_str.splitlines()):
            token = line.strip()
            if token:
                self.vocab[token] = i
                
    def tokenize(self, text: str) -> list[str]:
        text = text.lower().strip()
        for char in [".", ",", "!", "?", "-", "_", "(", ")", "[", "]", "'", '"']:
            text = text.replace(char, f" {char} ")
        words = text.split()
        
        output_tokens = []
        for word in words:
            chars = list(word)
            is_bad = False
            start = 0
            sub_tokens = []
            while start < len(chars):
                end = len(chars)
                cur_substr = None
                while start < end:
                    substr = "".join(chars[start:end])
                    if start > 0:
                        substr = "##" + substr
                    if substr in self.vocab:
                        cur_substr = substr
                        break
                    end -= 1
                if cur_substr is None:
                    is_bad = True
                    break
                sub_tokens.append(cur_substr)
                start = end
            if is_bad:
                output_tokens.append("[UNK]")
            else:
                output_tokens.extend(sub_tokens)
        return output_tokens


class SemanticIntentClassifier:
    """
    An upgraded high-performance Vector Space Model (VSM) classifier that leverages our
    custom 30,522-word WordPieceTokenizer to perform sub-word tokenization,
    applies character-level spelling correction (Levenshtein Distance) for noisy dictation,
    normalises musical slang, and scales term vectors using custom IDF weighting.
    
    Fully compatible with Android (zero binary dependencies, runs in <1ms).
    """
    def __init__(self, _base_dir: str = ""):
        self.tokenizer = WordPieceTokenizer()
        
        # Mapping colloquial slang and abbreviations to canonical search terms
        self.slang_map = {
            "banger": "song",
            "bangers": "songs",
            "tune": "song",
            "tunes": "music",
            "track": "song",
            "tracks": "music",
            "blast": "play",
            "spin": "play",
            "crank": "play",
            "shove": "add",
            "tack": "add",
            "pile": "add",
            "quiet": "mute",
            "silence": "mute",
            "hush": "mute",
            "kill": "stop",
            "abort": "stop",
            "freeze": "pause",
            "mute": "pause",
            "unmute": "resume"
        }
        
        # Manual Inverse Document Frequency (IDF) custom scaling weights
        # High value = distinctive intent indicators; Low value = common grammatical particles
        self.idf_weights = {
            # Crucial command verbs and nouns
            "skip": 2.8,
            "next": 2.4,
            "previous": 2.4,
            "prev": 2.8,
            "pause": 2.8,
            "resume": 2.8,
            "stop": 2.2,
            "clear": 2.2,
            "mute": 2.8,
            "unmute": 2.8,
            "shuffle": 2.6,
            "playlist": 1.8,
            "queue": 1.5,
            
            # Common/Low-weight semantic fillers
            "play": 0.4,
            "some": 0.15,
            "music": 0.3,
            "song": 0.35,
            "a": 0.1,
            "an": 0.1,
            "the": 0.1,
            "to": 0.1,
            "me": 0.15,
            "for": 0.15,
            "on": 0.1,
            "please": 0.1,
            "jarvis": 0.1,
            "can": 0.1,
            "you": 0.1,
            "go": 0.2,
            "hey": 0.1,
            "yo": 0.1,
            "this": 0.2,
            "it": 0.1
        }
        
        self.anchors = {
            "play_now": [
                "play some music", "start playing a track", "put on a song", "play something", "play anything",
                "listen to some tunes", "crank up some tracks", "blast this banger",
                "spin some tunes", "crank this song", "gimme some music", "put on some tracks"
            ],
            "queue_add": [
                "add this to my queue", "shove this on the queue", "enqueue some tracks",
                "tack this onto the queue", "pile this behind the current song",
                "put this in queue", "throw this on the queue"
            ],

            "skip": [
                "skip this song", "play the next track", "go to the next song", "next please",
                "fast forward", "go forward", "next song please", "skip this banger"
            ],
            "pause": [
                "pause the playback", "stop the music temporarily", "hold on the music",
                "hold the music", "freeze the track", "wait a second"
            ],
            "resume": [
                "resume playing", "unpause the music", "start up the track again",
                "keep going", "keep playing", "start it up", "carry on"
            ],
            "stop": [
                "stop playing completely", "abort playback", "turn off the player",
                "kill the audio", "shut up", "turn it off", "cut it"
            ],
            "greet": [
                "hello", "hi", "how is it going", "what is up", "how are you doing", 
                "greetings", "good morning", "yo jarvis", "what's up", "hey there",
                "wake up jarvis", "how is my assistant"
            ],
            "help": [
                "what can you do", "show me your capabilities", "help me please",
                "what are your features", "how do i use jarvis", "what can you do for me today",
                "show help options", "explain your commands", "what are your skills",
                "show me commands", "help me jarvis"
            ],
            "play_the_usual": [
                "play the usual", "queue up the usual", "play my usual", "queue my usual songs"
            ],
            "creator": [
                "who made you", "who created you", "who programmed you", "who built you", "who is your father"
            ],
            "joke": [
                "tell me a joke", "say something funny", "make me laugh", "got any jokes"
            ],
            "time_date": [
                "what time is it", "what is the date", "what day is it", "tell me the time"
            ],
            "status": [
                "system status", "status report", "run diagnostics", "how are you doing"
            ],
            "thanks": [
                "thank you", "thanks jarvis", "good job", "cheers", "awesome"
            ],
            "quote": [
                "give me a quote", "say something wise", "quote me"
            ]
        }
        
        # Pre-compute anchor tokens at startup for instant 0ms classification
        self.anchor_tokens = {}
        for intent, phrases in self.anchors.items():
            self.anchor_tokens[intent] = []
            for phrase in phrases:
                # Apply slang mapping during anchor compilation
                mapped_phrase_words = []
                for w in phrase.lower().split():
                    mapped_phrase_words.append(self.slang_map.get(w, w))
                mapped_phrase = " ".join(mapped_phrase_words)
                
                tokens = self.tokenizer.tokenize(mapped_phrase)
                # Compute IDF-weighted frequency vector
                vec = {}
                for t in tokens:
                    weight = self.idf_weights.get(t, 1.0)
                    vec[t] = vec.get(t, 0.0) + weight
                norm = math.sqrt(sum(v**2 for v in vec.values()))
                self.anchor_tokens[intent].append((phrase, vec, norm))

    def classify(self, text: str, threshold: float = 0.45) -> Optional[Tuple[str, str, float]]:
        # Apply spelling correction and slang mapping on the search query
        mapped_words = []
        for word in text.lower().split():
            clean_word = word.strip(".,!?\"'()[]")
            
            # Fast spelling correction check for short crucial command words
            if len(clean_word) >= 3:
                for target in ["skip", "pause", "resume", "mute", "unmute", "queue", "playlist"]:
                    if levenshtein_distance(clean_word, target) == 1:
                        clean_word = target
                        break
            
            mapped_words.append(self.slang_map.get(clean_word, clean_word))
            
        mapped_text = " ".join(mapped_words)
        query_tokens = self.tokenizer.tokenize(mapped_text)
        if not query_tokens:
            return None
            
        q_vec = {}
        for t in query_tokens:
            weight = self.idf_weights.get(t, 1.0)
            q_vec[t] = q_vec.get(t, 0.0) + weight
        q_norm = math.sqrt(sum(v**2 for v in q_vec.values()))
        if q_norm == 0:
            return None
            
        best_intent = None
        best_phrase = None
        best_score = -1.0
        
        for intent, phrase_data in self.anchor_tokens.items():
            for phrase, a_vec, a_norm in phrase_data:
                # Dot product
                dot_product = 0.0
                for token, q_val in q_vec.items():
                    if token in a_vec:
                        dot_product += q_val * a_vec[token]
                
                # Cosine Similarity
                cosine = dot_product / (q_norm * a_norm) if (q_norm * a_norm) > 0 else 0.0
                
                # Substring match boost
                substring_boost = 0.0
                if phrase.lower() in text.lower() or text.lower() in phrase.lower():
                    substring_boost = 0.15
                    
                score = cosine + substring_boost
                score = min(score, 1.0)
                
                if score > best_score:
                    best_score = score
                    best_intent = intent
                    best_phrase = phrase
                    
        if best_score >= threshold:
            return best_intent, best_phrase, best_score
        return None

    def extract_slots(self, text: str, intent: str) -> Optional[str]:
        clean = text.lower().strip()
        
        prev = None
        while prev != clean:
            prev = clean
            for filler in ("jarvis", "please", "can you", "could you", "just", "hey", "okay", "will you", "would you"):
                if clean.startswith(filler):
                    clean = clean[len(filler):].strip()
            for filler in ("please", "thanks", "thank you", "now", "sir", "next"):
                if clean.endswith(filler):
                    clean = clean[:-len(filler)].strip()
                
        if intent == "play_now":
            for verb in ("play some", "play a", "play", "start playing", "put on", "listen to", "crank up", 
                         "blast some", "blast a", "blast", "spin some", "spin a", "spin", "crank up some", "crank up a", "crank up"):
                if clean.startswith(verb):
                    clean = clean[len(verb):].strip()
            # Clean leading filler nouns like "banger", "song", "track", "tune"
            for filler_noun in ("banger", "song", "track", "tune"):
                if clean.startswith(filler_noun):
                    clean = clean[len(filler_noun):].strip()
            for suffix in ("please", "for me", "now", "banger"):
                if clean.endswith(suffix):
                    clean = clean[:-len(suffix)].strip()
            return clean if clean else None
            
        elif intent == "queue_add":
            for verb in ("add this to my", "add", "shove", "enqueue", "tack", "pile", "throw", "put"):
                if clean.startswith(verb):
                    clean = clean[len(verb):].strip()
            for suffix in ("to my queue", "on the queue", "onto the queue", "to queue", "in the queue", "into the queue"):
                if clean.endswith(suffix):
                    clean = clean[:-len(suffix)].strip()
            return clean if clean else None

            
        return None
