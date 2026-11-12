import FormalConjectures.Util.ProblemImports
import Mathlib.Data.Nat.Prime.Basic

/-!
# Goldbach-shaped stub

A fixture that looks like a formal-conjectures file: imports, a module
doc comment, a `sorry` that is only inside a comment, a proved lemma,
and one real hole. This is original fixture text, not a copy of the
upstream repository.
-/

namespace FormalConjectures

/-- A comment that happens to contain the word sorry should not be a hole. -/
-- this sorry is in a line comment and must be ignored

/-- Already proved; no hole. -/
theorem already_shown : True := by
  trivial

/-- The actual conjecture-shaped hole. -/
theorem even_gt_two_is_sum_of_two_primes (n : ℕ) (hn : Even n) (h2 : 2 < n) :
    ∃ p q : ℕ, Nat.Prime p ∧ Nat.Prime q ∧ p + q = n := by
  sorry

end FormalConjectures
