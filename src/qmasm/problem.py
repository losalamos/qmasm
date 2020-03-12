###################################
# Define an Ising or QUBO problem #
# By Scott Pakin <pakin@lanl.gov> #
###################################

import copy
import dimod
import sys
from collections import defaultdict
from qmasm.assertions import AssertParser

# Problem is currently just a thin veneer over dimod.BinaryQuadraticModel.  If
# it turns out we don't even need this veneer, we may replace it with direct
# use of dimod.BinaryQuadraticModel.
class Problem(object):
    "Represent either an Ising or QUBO problem."

    def __init__(self, qmasm, qubo):
        self.qmasm = qmasm   # Pointer to the top-level QMASM class
        self.qubo = qubo     # True=QUBO; False=Ising
        self.weights = defaultdict(lambda: 0.0)    # Map from a spin to a point weight
        self.strengths = defaultdict(lambda: 0.0)  # Map from a pair of spins to a coupler strength
        self.chains = set()       # Subset of strengths keys that represents user-defined chains (always logical)
        self.antichains = set()   # Subset of strengths keys that represents user-defined anti-chains (always logical)
        self.assertions = []      # List of assertions (as ASTs) to enforce
        self.pending_asserts = [] # List of {string, op, string} tuples pending conversion to assertions
        self.pinned = []          # Pairs of {unique number, Boolean} to pin
        self.known_values = {}    # Map from a spin to -1 or +1
        self.bqm_vars = None      # Set of all variables appearing in the BQM
        self.contractions = {}    # Map from a spin to another spin it must be equal to

    def assign_chain_strength(self, ch_str):
        """Define a strength for each user-specified and automatically
        generated chain, and assign strengths to those chains (and negative
        strength to all anti-chains).  Return the computed chain strength."""
        chain_strength = ch_str
        if chain_strength == None:
            # Chain strength defaults to twice the maximum strength in the data.
            try:
                chain_strength = -2*max([abs(w) for w in self.strengths.values()])
            except ValueError:
                # No strengths -- use weights instead.
                try:
                    chain_strength = -2*max([abs(w) for w in self.weights.values()])
                except ValueError:
                    # No weights or strengths -- arbitrarily choose -1.
                    chain_strength = -1.0
        elif self.qubo:
            # With QUBO input we need to divide the chain strength by 4 for
            # consistency with the other coupler strengths.
            chain_strength /= 4.0
        for c in self.chains:
            self.strengths[c] += chain_strength
        for c in self.antichains:
            self.strengths[c] -= chain_strength
        return chain_strength

    def generate_bqm(self):
        "Generate a BinaryQuadraticModel version of the Problem."
        # Create a BQM.
        btype = dimod.SPIN
        if self.qubo:
            btype = dimod.BINARY
        bqm = dimod.BinaryQuadraticModel(self.weights, self.strengths, 0, btype, problem=self)
        if self.qubo:
            bqm.change_vartype(dimod.SPIN, inplace=True)

        # Pin all variables the user asked to pin.
        bool2spin = {False: -1, True: +1}
        pins = {q: bool2spin[b] for q, b in self.pinned}
        for q in pins:
            # Ensure that every pinned variable exists.  Otherwise,
            # fix_variables will throw a KeyError.
            bqm.add_variable(q, 0)
        bqm.fix_variables(pins)

        # Store the BQM.
        self.bqm = bqm

    def all_bqm_variables(self, force_recompute=False):
        "Return a set of all variables, referenced in linear and/or quadratic terms in the BQM."
        if self.bqm_vars != None and not force_recompute:
            return self.bqm_vars
        self.bqm_vars = set(self.bqm.linear)
        for u, v in self.bqm.quadratic:
            self.bqm_vars.add(u)
            self.bqm_vars.add(v)
        return self.bqm_vars

    def _edges_to_adj_list(self, edges):
        "Convert a list of edges to an adjacency list."
        adj = defaultdict(lambda: [])
        for u, v in edges:
            adj[u].append(v)
            adj[v].append(u)
        return adj

    def _traversal_from_root(self, adj, visited, root):
        """"Return a reversed traversal order of an adjacency list from a
        given root such that each right vertex is a leaf if all
        preceding right vertices are removed."""
        order = []
        new_visited = visited.copy()
        new_visited.add(root)
        for v in adj[root]:
            if v in new_visited:
                continue
            order.append((root, v))
            ord, vis = self._traversal_from_root(adj, new_visited, v)
            order.extend(ord)
            new_visited.update(vis)
        return order, new_visited

    def traversal_order(self, edges):
        """"Return a reversed traversal order of a graph such that each right
        vertex is a leaf if all preceding right vertices are removed."""
        adj = self._edges_to_adj_list(edges)
        order = []
        nodes = set()
        for u, v in edges:
            nodes.add(u)
            nodes.add(v)
        visited = set()
        for u in nodes:
            if u in visited:
                continue
            ord, vis = self._traversal_from_root(adj, visited, u)
            order.extend(ord)
            visited.update(vis)
        order.reverse()
        return order

    def convert_chains_to_aliases(self, verbosity):
        "Replace user-defined chains with aliases."
        # Say what we're about to do
        if verbosity >= 2:
            sys.stderr.write("Replaced user-defined chains with aliases:\n\n")
            sys.stderr.write("  %6d logical qubits before optimization\n" % len(self.all_bqm_variables()))

        # At the time of this writing, a BinaryQuadraticModel elides variables
        # with a weight of zero.  But then contract_variables complains that
        # the variable can't be found.  Hence, we add back all zero-valued
        # variables just to keep contract_variables from failing.
        self.bqm.add_variables_from({q[0]: 0 for q in self.chains})
        self.bqm.add_variables_from({q[1]: 0 for q in self.chains})

        # Remove variables that are made equivalent to other variable via
        # user-defined chains.
        order = self.traversal_order(self.chains)
        for u, v in order:
            self.bqm.contract_variables(u, v)
            self.contractions[v] = u

        # Say what we just did.
        if verbosity >= 2:
            sys.stderr.write("  %6d logical qubits after optimization\n\n" % len(self.all_bqm_variables(force_recompute=True)))

    def simplify_problem(self, verbosity):
        "Find and remove variables with known outputs."
        # Say what we're going to do.
        if verbosity >= 2:
            sys.stderr.write("Simplified the problem:\n\n")
            sys.stderr.write("  %6d logical qubits before optimization\n" % len(self.all_bqm_variables()))

        # Simplify the BQM.
        self.known_values = dimod.roof_duality.fix_variables(self.bqm, True)
        self.bqm.fix_variables(self.known_values)

        # Say what we just did.
        if verbosity >= 2:
            num_left = len(self.all_bqm_variables(force_recompute=True))
            sys.stderr.write("  %6d logical qubits after optimization\n\n" % num_left)
            if num_left == 0:
                sys.stderr.write("    Note: A complete solution can be found classically using roof duality.\n\n")

    def append_assertions_from_statements(self):
        "Convert user-specified chains, anti-chains, and pins to assertions."
        # Convert pending assertions to actual assertions.
        # TODO: Quote variables containing special characters.
        ap = AssertParser(self.qmasm)
        for s1, op, s2 in self.pending_asserts:
            ast = ap.parse(s1 + " " + op + " " + s2)
            ast.compile()
            self.assertions.append(ast)

    def merged_known_values(self):
        "Merge pinned, known_values, and chains into a s single dictionary."
        merged = {k: v for k, v in self.pinned}
        spin2bool = {-1: False, +1: True}
        for k, v in self.known_values.items():
            merged[k] = spin2bool[v]
        remaining = copy.copy(self.chains)
        while len(remaining) > 0:
            still_remaining = set()
            for q0, q1 in remaining:
                if q0 in merged:
                    merged[q1] = merged[q0]
                elif q1 in merged:
                    merged[q0] = merged[q1]
                else:
                    still_remaining.add((q0, q1))
            if len(still_remaining) == len(remaining):
                break  # No progress was made.
            remaining = still_remaining
        return merged

    def dangling_variables(self, num2syms):
        "Return a set of variables that are neither embedded nor have a known value."
        dangling = set()
        known_values = self.merged_known_values()
        for i in range(len(num2syms)):
            if num2syms[i] == []:
                continue
            if i not in self.embedding and i not in known_values and i not in self.contractions:
                dangling.update(num2syms[i])
        return dangling

    def _output_embedding_verbosely(self, max_sym_name_len, num2syms):
        "Verbosely output the mapping from logical to physical qubits."
        sys.stderr.write("Established a mapping from logical to physical qubits:\n\n")
        sys.stderr.write("    Logical  %-*s  Physical\n" % (max_sym_name_len, "Name(s)"))
        sys.stderr.write("    -------  %s  -------------------------\n" % ("-" * max_sym_name_len))
        known_values = self.merged_known_values()
        pin_map = {k: v for k, v in self.pinned}
        for i in range(len(num2syms)):
            if num2syms[i] == []:
                continue
            name_list = " ".join(sorted(num2syms[i]))
            try:
                phys_list = " ".join(["%4d" % e for e in sorted(self.embedding[i])])
            except KeyError:
                try:
                    phys_list = "[Pinned to %s]" % repr(pin_map[i])
                except KeyError:
                    try:
                        phys_list = "[Provably %s]" % known_values[i]
                    except KeyError:
                        try:
                            phys_list = "[Same as logical %d]" % self.contractions[i]
                        except KeyError:
                            phys_list = "[Disconnected]"

            sys.stderr.write("    %7d  %-*s  %s\n" % (i, max_sym_name_len, name_list, phys_list))
        sys.stderr.write("\n")

    def _output_embedding_tersely(self, max_sym_name_len, num2syms):
        "Tersely output the mapping from logical to physical qubits."
        log2phys_comments = []
        known_values = self.merged_known_values()
        pin_map = {k: v for k, v in self.pinned}
        for i in range(len(num2syms)):
            if num2syms[i] == []:
                continue
            name_list = " ".join(sorted(num2syms[i]))
            try:
                phys_list = " ".join(["%4d" % e for e in sorted(self.embedding[i])])
            except KeyError:
                try:
                    phys_list = "[%s]" % repr(pin_map[i])
                except KeyError:
                    try:
                        phys_list = "[%s]" % known_values[i]
                    except KeyError:
                        try:
                            phys_list = "[Same as logical %d]" % self.contractions[i]
                        except KeyError:
                            phys_list = "[Disconnected]"
            log2phys_comments.append("# %s --> %s" % (name_list, phys_list))
        log2phys_comments.sort()
        sys.stderr.write("\n".join(log2phys_comments) + "\n")

    def output_embedding(self, verbosity, max_sym_name_len, num2syms):
        "Output the mapping from logical to physical qubits."
        if verbosity > 0:
            self._output_embedding_verbosely(max_sym_name_len, num2syms)
        else:
            self._output_embedding_tersely(max_sym_name_len, num2syms)

    def output_embedding_statistics(self):
        "Output various statistics about the embedding."
        # Tally the original set of variables.
        all_vars = set(self.weights)
        for q1, q2 in self.strengths:
            all_vars.add(q1)
            all_vars.add(q2)

        # Output statistics.
        known_true = sum([v == 1 for v in self.known_values.values()])
        known_false = len(self.known_values) - known_true
        sys.stderr.write("Computed the following statistics of the logical-to-physical mapping:\n\n")
        sys.stderr.write("    Type      Metric           Value\n")
        sys.stderr.write("    --------  ---------------  -----\n")
        sys.stderr.write("    Logical   Variables        %5d\n" % len(all_vars))
        sys.stderr.write("    Logical     Provably true  %5d\n" % known_true)
        sys.stderr.write("    Logical     Provably false %5d\n" % known_false)
        sys.stderr.write("    Logical   Strengths        %5d\n" % len(self.strengths))
        sys.stderr.write("    Logical     Equalities     %5d\n" % len(self.chains))
        sys.stderr.write("    Logical     Inequalities   %5d\n" % len(self.antichains))
        sys.stderr.write("    Physical  Spins            %5d\n" % len(self.embedded_bqm.linear))
        sys.stderr.write("    Physical  Couplers         %5d\n" % len(self.embedded_bqm.quadratic))
        sys.stderr.write("\n")

        # Output some additional chain statistics.
        chain_lens = [len(c) for c in self.embedding.values()]
        max_chain_len = 0
        if chain_lens != []:
            max_chain_len = max(chain_lens)
        num_max_chains = len([l for l in chain_lens if l == max_chain_len])
        sys.stderr.write("    Maximum chain length = %d (occurrences = %d)\n\n" % (max_chain_len, num_max_chains))
