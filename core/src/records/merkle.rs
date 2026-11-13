//! The Merkle tree over record hashes: RFC 6962 section 2.1, verified as in RFC 9162 section 2.1.
//!
//! Leaf i is the hash of record i. As in RFC 6962:
//!
//! ```text
//! leaf node  = SHA-256(0x00 || record hash)
//! inner node = SHA-256(0x01 || left || right)
//! empty tree = SHA-256("")
//! ```
//!
//! and a tree of n > 1 leaves splits at the largest power of two below n.
//! The Python twin is `muse/records/merkle.py`.

use sha2::{Digest, Sha256};

use super::{Hash, Kind, Problem};

/// The most elements an inclusion proof may have (a tree of 2^53 records needs 53).
pub const MAX_INCLUSION_PROOF: usize = 64;
/// The most elements a consistency proof may have.
pub const MAX_CONSISTENCY_PROOF: usize = 128;

fn sha(parts: &[&[u8]]) -> Hash {
    let mut h = Sha256::new();
    for part in parts {
        h.update(part);
    }
    h.finalize().into()
}

/// The leaf node for a record hash.
pub fn leaf_hash(record_hash: &Hash) -> Hash {
    sha(&[&[0u8], record_hash])
}

fn node(left: &Hash, right: &Hash) -> Hash {
    sha(&[&[1u8], left, right])
}

fn empty_root() -> Hash {
    sha(&[])
}

/// The largest power of two strictly below n (n > 1).
fn split(n: usize) -> usize {
    let mut k = 1;
    while k * 2 < n {
        k *= 2;
    }
    k
}

fn root_of(leaves: &[Hash]) -> Hash {
    match leaves.len() {
        0 => empty_root(),
        1 => leaves[0],
        n => {
            let k = split(n);
            node(&root_of(&leaves[..k]), &root_of(&leaves[k..]))
        }
    }
}

fn leaves_of(hashes: &[Hash]) -> Vec<Hash> {
    hashes.iter().map(leaf_hash).collect()
}

/// The root over the record hashes, in order.
pub fn merkle_root(hashes: &[Hash]) -> Hash {
    root_of(&leaves_of(hashes))
}

fn path(m: usize, leaves: &[Hash], out: &mut Vec<Hash>) {
    let n = leaves.len();
    if n == 1 {
        return;
    }
    let k = split(n);
    if m < k {
        path(m, &leaves[..k], out);
        out.push(root_of(&leaves[k..]));
    } else {
        path(m - k, &leaves[k..], out);
        out.push(root_of(&leaves[..k]));
    }
}

/// The audit path for record `index` in the tree over all of `hashes`.
pub fn inclusion_proof(hashes: &[Hash], index: u64) -> Result<Vec<Hash>, Problem> {
    if index >= hashes.len() as u64 {
        return Err(Kind::Malformed.into());
    }
    let mut out = Vec::new();
    path(index as usize, &leaves_of(hashes), &mut out);
    Ok(out)
}

/// Is the record with this hash number `index` of the `size` records under `root`?
pub fn verify_inclusion(record_hash: &Hash, index: u64, size: u64, proof: &[Hash], root: &Hash) -> bool {
    if index >= size || proof.len() > MAX_INCLUSION_PROOF {
        return false;
    }
    let (mut f, mut s, mut r) = (index, size - 1, leaf_hash(record_hash));
    for p in proof {
        if s == 0 {
            return false;
        }
        if f & 1 == 1 || f == s {
            r = node(p, &r);
            if f & 1 == 0 {
                while f != 0 && f & 1 == 0 {
                    f >>= 1;
                    s >>= 1;
                }
            }
        } else {
            r = node(&r, p);
        }
        f >>= 1;
        s >>= 1;
    }
    s == 0 && r == *root
}

fn subproof(m: usize, leaves: &[Hash], whole: bool, out: &mut Vec<Hash>) {
    let n = leaves.len();
    if m == n {
        if !whole {
            out.push(root_of(leaves));
        }
        return;
    }
    let k = split(n);
    if m <= k {
        subproof(m, &leaves[..k], whole, out);
        out.push(root_of(&leaves[k..]));
    } else {
        subproof(m - k, &leaves[k..], false, out);
        out.push(root_of(&leaves[..k]));
    }
}

/// The proof that the first `old_size` records are a prefix of all of `hashes`.
/// Empty when `old_size` is 0 or equals the number of hashes (nothing needs proving).
pub fn consistency_proof(hashes: &[Hash], old_size: u64) -> Result<Vec<Hash>, Problem> {
    if old_size > hashes.len() as u64 {
        return Err(Kind::Malformed.into());
    }
    let mut out = Vec::new();
    if old_size != 0 && old_size != hashes.len() as u64 {
        subproof(old_size as usize, &leaves_of(hashes), true, &mut out);
    }
    Ok(out)
}

/// Does the tree of `new_size` records under `new_root` extend the tree of `old_size` under `old_root`?
pub fn verify_consistency(old_size: u64, new_size: u64, old_root: &Hash, new_root: &Hash, proof: &[Hash]) -> bool {
    if old_size > new_size || proof.len() > MAX_CONSISTENCY_PROOF {
        return false;
    }
    if old_size == new_size {
        return proof.is_empty() && old_root == new_root;
    }
    if old_size == 0 {
        return proof.is_empty() && *old_root == empty_root();
    }
    let mut path: Vec<&Hash> = Vec::with_capacity(proof.len() + 1);
    if old_size.is_power_of_two() {
        path.push(old_root);
    }
    path.extend(proof.iter());
    let Some((first, rest)) = path.split_first() else {
        return false;
    };
    let (mut f, mut s) = (old_size - 1, new_size - 1);
    while f & 1 == 1 {
        f >>= 1;
        s >>= 1;
    }
    let (mut fr, mut sr) = (**first, **first);
    for c in rest {
        if s == 0 {
            return false;
        }
        if f & 1 == 1 || f == s {
            fr = node(c, &fr);
            sr = node(c, &sr);
            if f & 1 == 0 {
                while f != 0 && f & 1 == 0 {
                    f >>= 1;
                    s >>= 1;
                }
            }
        } else {
            sr = node(&sr, c);
        }
        f >>= 1;
        s >>= 1;
    }
    s == 0 && fr == *old_root && sr == *new_root
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leaves(n: usize) -> Vec<Hash> {
        (0..n).map(|i| sha(&[format!("leaf {i}").as_bytes()])).collect()
    }

    #[test]
    fn empty_and_single() {
        // RFC 6962: the hash of an empty tree is SHA-256 of nothing.
        assert_eq!(super::super::to_hex(&merkle_root(&[])), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        let one = leaves(1);
        assert_eq!(merkle_root(&one), leaf_hash(&one[0]));
    }

    #[test]
    fn every_inclusion_proof_verifies_only_for_its_leaf() {
        for n in 1..=70usize {
            let all = leaves(n);
            let root = merkle_root(&all);
            for i in 0..n {
                let proof = inclusion_proof(&all, i as u64).unwrap();
                assert!(verify_inclusion(&all[i], i as u64, n as u64, &proof, &root), "{i} of {n}");
                for j in 0..n {
                    if j != i {
                        assert!(!verify_inclusion(&all[j], i as u64, n as u64, &proof, &root), "leaf {j} with the proof for {i} of {n}");
                        assert!(!verify_inclusion(&all[i], j as u64, n as u64, &proof, &root), "index {j} with the proof for {i} of {n}");
                    }
                }
            }
        }
    }

    #[test]
    fn every_consistency_proof_verifies_and_a_changed_prefix_does_not() {
        for n in 0..=70usize {
            let all = leaves(n);
            let root = merkle_root(&all);
            for m in 0..=n {
                let proof = consistency_proof(&all, m as u64).unwrap();
                let old = merkle_root(&all[..m]);
                assert!(verify_consistency(m as u64, n as u64, &old, &root, &proof), "{m} -> {n}");
                if m > 0 {
                    let mut other = all.clone();
                    other[m - 1][0] ^= 1;
                    let changed = merkle_root(&other);
                    let changed_proof = consistency_proof(&other, m as u64).unwrap();
                    assert!(!verify_consistency(m as u64, n as u64, &old, &changed, &changed_proof), "rewritten {m} -> {n}");
                }
            }
        }
    }
}
