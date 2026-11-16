//! BIP-39 mnemonics: entropy to words and back, in four languages.
//!
//! Only the *encoding* half of BIP-39 is used: the words stand for the entropy
//! plus its checksum. The PBKDF2 "seed" step is not used, so a Muse recovery
//! key is not a wallet seed and must never be entered into a wallet.
//!
//! The lists in `wordlists/` are the official ones from
//! <https://github.com/bitcoin/bips/tree/master/bip-0039>; their SHA-256 is
//! checked in the tests below.

use std::collections::HashMap;
use std::sync::OnceLock;

use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use zeroize::{Zeroize, Zeroizing};

/// A language with an official BIP-39 list that Muse offers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Language {
    English,
    ChineseTraditional,
    Japanese,
    Korean,
}

impl Language {
    pub const ALL: [Language; 4] =
        [Language::English, Language::ChineseTraditional, Language::Japanese, Language::Korean];

    /// The name used in files and vectors (the official list's file name without `.txt`).
    pub fn name(self) -> &'static str {
        match self {
            Language::English => "english",
            Language::ChineseTraditional => "chinese_traditional",
            Language::Japanese => "japanese",
            Language::Korean => "korean",
        }
    }

    pub fn from_name(name: &str) -> Option<Language> {
        Language::ALL.into_iter().find(|l| l.name() == name)
    }

    /// The official list file, byte for byte.
    pub fn list_text(self) -> &'static str {
        match self {
            Language::English => include_str!("wordlists/english.txt"),
            Language::ChineseTraditional => include_str!("wordlists/chinese_traditional.txt"),
            Language::Japanese => include_str!("wordlists/japanese.txt"),
            Language::Korean => include_str!("wordlists/korean.txt"),
        }
    }

    /// What goes between words when they are shown: an ideographic space for
    /// Japanese (as BIP-39 asks), a plain space otherwise.
    pub fn separator(self) -> &'static str {
        match self {
            Language::Japanese => "\u{3000}",
            _ => " ",
        }
    }

    fn table(self) -> &'static Table {
        static TABLES: [OnceLock<Table>; 4] = [OnceLock::new(), OnceLock::new(), OnceLock::new(), OnceLock::new()];
        let slot = Language::ALL.iter().position(|l| *l == self).unwrap_or(0);
        TABLES[slot].get_or_init(|| {
            let words: Vec<&'static str> = self.list_text().lines().collect();
            // The official files are already NFKD; normalising again keeps lookups right
            // even if a list were ever replaced by a differently composed copy.
            let index = words.iter().enumerate().map(|(i, w)| (w.nfkd().collect::<String>(), i as u16)).collect();
            let shown = words.iter().map(|w| w.nfc().collect()).collect();
            Table { shown, index }
        })
    }

    /// The word at `index` (0..2048), composed (NFC) for showing and printing.
    /// The official Japanese and Korean lists are decomposed (NFKD), which
    /// some fonts and printers render badly; reading words back normalises
    /// either form.
    pub fn word(self, index: u16) -> Option<&'static str> {
        self.table().shown.get(index as usize).map(String::as_str)
    }

    /// The index of a word, after NFKD normalisation (and ASCII lower-casing for English).
    pub fn index_of(self, word: &str) -> Option<u16> {
        let mut key: String = word.nfkd().collect();
        if self == Language::English {
            key.make_ascii_lowercase();
        }
        let found = self.table().index.get(key.as_str()).copied();
        key.zeroize();
        found
    }
}

struct Table {
    /// The same words composed (NFC).
    shown: Vec<String>,
    index: HashMap<String, u16>,
}

/// Why words could not be read. Never contains the words.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MnemonicError {
    /// Not 12, 15, 18, 21 or 24 words (a recovery key needs 24).
    WordCount,
    /// The word at this 1-based position is not in the list for the language.
    UnknownWord(usize),
    /// Every word is in the list but the checksum does not match: a word is
    /// wrong, or two are swapped.
    Checksum,
    /// Entropy must be 16, 20, 24, 28 or 32 bytes.
    EntropyLength,
}

impl std::fmt::Display for MnemonicError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MnemonicError::WordCount => f.write_str("wrong number of words"),
            MnemonicError::UnknownWord(position) => write!(f, "word {position} is not in the word list"),
            MnemonicError::Checksum => f.write_str("the words do not match their checksum"),
            MnemonicError::EntropyLength => f.write_str("entropy has an unsupported length"),
        }
    }
}

impl std::error::Error for MnemonicError {}

/// Word indices (11 bits each) for `entropy` followed by its checksum bits.
pub fn entropy_to_indices(entropy: &[u8]) -> Result<Zeroizing<Vec<u16>>, MnemonicError> {
    if !matches!(entropy.len(), 16 | 20 | 24 | 28 | 32) {
        return Err(MnemonicError::EntropyLength);
    }
    let mut bytes = Zeroizing::new(entropy.to_vec());
    bytes.push(Sha256::digest(entropy)[0]);
    let count = entropy.len() * 3 / 4; // (8n + n/4) / 11
    let mut out = Zeroizing::new(Vec::with_capacity(count));
    for word in 0..count {
        let mut value = 0u16;
        for bit in word * 11..word * 11 + 11 {
            value = value << 1 | u16::from(bytes[bit / 8] >> (7 - bit % 8) & 1);
        }
        out.push(value);
    }
    Ok(out)
}

/// The entropy behind word indices, after checking the checksum.
pub fn indices_to_entropy(indices: &[u16]) -> Result<Zeroizing<Vec<u8>>, MnemonicError> {
    if !matches!(indices.len(), 12 | 15 | 18 | 21 | 24) {
        return Err(MnemonicError::WordCount);
    }
    let entropy_len = indices.len() * 4 / 3;
    let mut bytes = Zeroizing::new(vec![0u8; entropy_len + 1]);
    for (word, index) in indices.iter().enumerate() {
        for offset in 0..11 {
            if index >> (10 - offset) & 1 == 1 {
                let bit = word * 11 + offset;
                bytes[bit / 8] |= 1 << (7 - bit % 8);
            }
        }
    }
    let checksum_bits = entropy_len / 4;
    let mask = 0xffu8 << (8 - checksum_bits);
    let expected = Sha256::digest(&bytes[..entropy_len])[0] & mask;
    if bytes[entropy_len] & mask != expected {
        return Err(MnemonicError::Checksum);
    }
    Ok(Zeroizing::new(bytes[..entropy_len].to_vec()))
}

/// The words for `entropy`, joined with the language's separator.
pub fn entropy_to_words(language: Language, entropy: &[u8]) -> Result<Zeroizing<String>, MnemonicError> {
    let indices = entropy_to_indices(entropy)?;
    let mut out = Zeroizing::new(String::new());
    for (i, index) in indices.iter().enumerate() {
        if i > 0 {
            out.push_str(language.separator());
        }
        out.push_str(language.word(*index).unwrap_or(""));
    }
    Ok(out)
}

/// Splits typed text into words: NFKD first (so an ideographic space becomes a
/// plain one and composed Hangul or kana match the list), then on white space.
/// Chinese words are single characters, so Chinese text may be typed with or
/// without spaces.
pub fn split_words(language: Language, text: &str) -> Zeroizing<Vec<String>> {
    let normalised: Zeroizing<String> = Zeroizing::new(text.nfkd().collect());
    let mut out = Zeroizing::new(Vec::new());
    for token in normalised.split_whitespace() {
        if language == Language::ChineseTraditional {
            out.extend(token.chars().map(String::from));
        } else {
            out.push(token.to_string());
        }
    }
    out
}

/// The word indices of typed text in one language (no checksum check).
pub fn words_to_indices(language: Language, text: &str) -> Result<Zeroizing<Vec<u16>>, MnemonicError> {
    let mut words = split_words(language, text);
    let mut out = Zeroizing::new(Vec::with_capacity(words.len()));
    let mut failure = None;
    for (i, word) in words.iter().enumerate() {
        match language.index_of(word) {
            Some(index) => out.push(index),
            None => {
                failure = Some(MnemonicError::UnknownWord(i + 1));
                break;
            }
        }
    }
    for word in words.iter_mut() {
        word.zeroize();
    }
    match failure {
        Some(error) => Err(error),
        None => Ok(out),
    }
}

/// The entropy for typed words in one language.
pub fn words_to_entropy(language: Language, text: &str) -> Result<Zeroizing<Vec<u8>>, MnemonicError> {
    let indices = words_to_indices(language, text)?;
    indices_to_entropy(&indices)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vault::secret::sha256_hex;

    /// SHA-256 of the four official list files. The same four values were
    /// obtained from three places on 2026-11-13: github.com/bitcoin/bips
    /// (bip-0039/*.txt), github.com/trezor/python-mnemonic
    /// (src/mnemonic/wordlist/*.txt) and, word for word, the `bip39` crate 2.2.0.
    const PUBLISHED: [(Language, &str); 4] = [
        (Language::English, "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"),
        (Language::ChineseTraditional, "417b26b3d8500a4ae3d59717d7011952db6fc2fb84b807f3f94ac734e89c1b5f"),
        (Language::Japanese, "2eed0aef492291e061633d7ad8117f1a2b03eb80a29d0e4e3117ac2528d05ffd"),
        (Language::Korean, "9e95f86c167de88f450f0aaf89e87f6624a57f973c67b516e338e8e8b8897f60"),
    ];

    #[test]
    fn word_lists_are_the_official_files() {
        for (language, digest) in PUBLISHED {
            assert_eq!(sha256_hex(language.list_text().as_bytes()), digest, "{}", language.name());
            let table = language.table();
            let words: Vec<&str> = language.list_text().lines().collect();
            assert_eq!(words.len(), 2048);
            assert_eq!(table.index.len(), 2048, "{}: words collide after NFKD", language.name());
            for (i, word) in words.iter().enumerate() {
                assert_eq!(language.index_of(word), Some(i as u16));
                assert_eq!(word.nfkd().collect::<String>(), *word, "the official lists are in NFKD form");
            }
        }
    }

    #[test]
    fn no_word_belongs_to_two_languages() {
        for a in Language::ALL {
            for b in Language::ALL {
                if a != b {
                    assert!(a.list_text().lines().all(|w| b.index_of(w).is_none()), "{} / {}", a.name(), b.name());
                }
            }
        }
    }
}
