"""校验受控语料题集与证据范围；具体检查位于 scripts.support。"""

from scripts.support.verified_dataset import answer_atoms, main, mutation_checks, readable, verify

__all__ = ["answer_atoms", "main", "mutation_checks", "readable", "verify"]

if __name__ == "__main__":
    main()
