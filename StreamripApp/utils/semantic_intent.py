import gzip
import base64
import math
from typing import Optional, Tuple

from utils.bge_vocab_data import EMBEDDED_VOCAB

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
    A high-performance Vector Space Model (VSM) classifier that leverages our
    custom 30,522-word WordPieceTokenizer to perform sub-word tokenization
    and compute sparse token-frequency Cosine Similarity.
    
    Fully compatible with Android (zero binary dependencies, runs in <1ms).
    """
    def __init__(self, _base_dir: str = ""):
        self.tokenizer = WordPieceTokenizer()
        self.anchors = {
            "play_now": [
                "play some music", "start playing a track", "put on a song", 
                "listen to some tunes", "crank up some tracks"
            ],
            "queue_add": [
                "add this to my queue", "shove this on the queue", "enqueue some tracks",
                "tack this onto the queue", "pile this behind the current song"
            ],
            "playlist_create": [
                "create a new playlist", "make a new playlist", "generate a blank playlist",
                "build an empty playlist", "start a blank playlist"
            ],
            "playlist_auto": [
                "create a smart mood playlist", "make a chill playlist", "build an upbeat playlist",
                "generate a dark mood playlist"
            ],
            "skip": [
                "skip this song", "play the next track", "go to the next song", "next please"
            ],
            "pause": [
                "pause the playback", "stop the music temporarily", "hold on the music"
            ],
            "resume": [
                "resume playing", "unpause the music", "start up the track again"
            ],
            "stop": [
                "stop playing completely", "abort playback", "turn off the player"
            ],
            "greet": [
                "hello", "hi", "how is it going", "what is up", "how are you doing", 
                "greetings", "good morning", "yo jarvis", "what's up", "hey there"
            ],
            "help": [
                "what can you do", "show me your capabilities", "help me please",
                "what are your features", "how do i use jarvis", "what can you do for me today",
                "show help options", "explain your commands"
            ]
        }
        
        # Pre-compute anchor tokens at startup for instant 0ms classification
        self.anchor_tokens = {}
        for intent, phrases in self.anchors.items():
            self.anchor_tokens[intent] = []
            for phrase in phrases:
                tokens = self.tokenizer.tokenize(phrase)
                # Frequency vector mapping
                vec = {}
                for t in tokens:
                    vec[t] = vec.get(t, 0) + 1
                norm = math.sqrt(sum(v**2 for v in vec.values()))
                self.anchor_tokens[intent].append((phrase, vec, norm))

    def classify(self, text: str, threshold: float = 0.50) -> Optional[Tuple[str, str, float]]:
        query_tokens = self.tokenizer.tokenize(text)
        if not query_tokens:
            return None
            
        q_vec = {}
        for t in query_tokens:
            q_vec[t] = q_vec.get(t, 0) + 1
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
            for verb in ("play some", "play a", "play", "start playing", "put on", "listen to", "crank up"):
                if clean.startswith(verb):
                    clean = clean[len(verb):].strip()
            for suffix in ("please", "for me", "now"):
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
            
        elif intent in ("playlist_create", "playlist_auto"):
            for verb in ("create a new playlist called", "create a playlist called", "create a playlist named",
                         "make a playlist called", "make a playlist named", "create a new playlist",
                         "make a new playlist", "generate a playlist", "create a", "make a", "build a"):
                if clean.startswith(verb):
                    clean = clean[len(verb):].strip()
            clean = clean.strip("\"'")
            return clean if clean else None
            
        return None
