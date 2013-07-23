"""Support for aggregation-based AMG"""

__docformat__ = "restructuredtext en"

from warnings import warn
import numpy as np
import scipy
from scipy.sparse import csr_matrix, isspmatrix_csr, isspmatrix_bsr, eye

from pyamg import relaxation
from pyamg import amg_core
from pyamg.multilevel import multilevel_solver
from pyamg.relaxation.smoothing import change_smoothers
from pyamg.util.utils import symmetric_rescaling_sa, amalgamate,\
    relaxation_as_linear_operator, eliminate_diag_dom_nodes, blocksize,\
    preprocess_improve_candidates, preprocess_smooth, preprocess_str_or_agg
from pyamg.strength import classical_strength_of_connection,\
    symmetric_strength_of_connection, evolution_strength_of_connection,\
    energy_based_strength_of_connection, distance_strength_of_connection,\
    algebraic_distance
from aggregate import standard_aggregation, naive_aggregation,\
    lloyd_aggregation
from tentative import fit_candidates
from smooth import jacobi_prolongation_smoother,\
    richardson_prolongation_smoother, energy_prolongation_smoother

__all__ = ['smoothed_aggregation_solver']

def smoothed_aggregation_solver(A, B=None, BH=None,
                                symmetry='hermitian', strength='symmetric',
                                aggregate='standard',
                                smooth=('jacobi', {'omega': 4.0/3.0}),
                                presmoother=('block_gauss_seidel',
                                             {'sweep': 'symmetric'}),
                                postsmoother=('block_gauss_seidel',
                                              {'sweep': 'symmetric'}),
                                improve_candidates='default', max_levels = 10,
                                max_coarse = 500,  
                                diagonal_dominance=False,
                                keep=False, **kwargs):
    """
    Create a multilevel solver using classical-style Smoothed Aggregation (SA)

    Parameters
    ----------
    A : {csr_matrix, bsr_matrix}
        Sparse NxN matrix in CSR or BSR format
    B : {None, array_like}
        Right near-nullspace candidates stored in the columns of an NxK array.
        The default value B=None is equivalent to B=ones((N,1))
    BH : {None, array_like}
        Left near-nullspace candidates stored in the columns of an NxK array.
        BH is only used if symmetry is 'nonsymmetric'.
        The default value B=None is equivalent to BH=B.copy()
    symmetry : {string}
        'symmetric' refers to both real and complex symmetric
        'hermitian' refers to both complex Hermitian and real Hermitian
        'nonsymmetric' i.e. nonsymmetric in a hermitian sense
        Note, in the strictly real case, symmetric and hermitian are the same
        Note, this flag does not denote definiteness of the operator.
    strength : {list} : default ['symmetric', 'classical', 'evolution',
               ('predefined', {'C' : csr_matrix}), None]
        Method used to determine the strength of connection between unknowns of
        the linear system.  Method-specific parameters may be passed in using a
        tuple, e.g. strength=('symmetric',{'theta' : 0.25 }). If strength=None,
        all nonzero entries of the matrix are considered strong.
        See notes below for varying this parameter on a per level basis.  Also,
        see notes below for using a predefined strength matrix on each level.
    aggregate : {list} : default ['standard', 'lloyd', 'naive',
                ('predefined', {'AggOp' : csr_matrix})]
        Method used to aggregate nodes.  See notes below for varying this
        parameter on a per level basis.  Also, see notes below for using a
        predefined aggregation on each level.
    smooth : {list} : default ['jacobi', 'richardson', 'energy', None]
        Method used to smooth the tentative prolongator.  Method-specific
        parameters may be passed in using a tuple, e.g.  smooth=
        ('jacobi',{'filter' : True }).  See notes below for varying this
        parameter on a per level basis.
    presmoother : {tuple, string, list} : default ('block_gauss_seidel',
                  {'sweep':'symmetric'})
        Defines the presmoother for the multilevel cycling.  The default block
        Gauss-Seidel option defaults to point-wise Gauss-Seidel, if the matrix
        is CSR or is a BSR matrix with blocksize of 1.  See notes below for
        varying this parameter on a per level basis.
    postsmoother : {tuple, string, list}
        Same as presmoother, except defines the postsmoother.
    improve_candidates : {list} : default
                        [('block_gauss_seidel', {'sweep':'symmetric'}), None]
        The ith entry defines the method used to improve the candidates B on
        level i.  If the list is shorter than max_levels, then the last entry
        will define the method for all levels lower.
        The list elements are relaxation descriptors of the form used for
        presmoother and postsmoother.  A value of None implies no action on B.
    max_levels : {integer} : default 10
        Maximum number of levels to be used in the multilevel solver.
    max_coarse : {integer} : default 500
        Maximum number of variables permitted on the coarse grid.
    diagonal_dominance : {bool, tuple} : default False
        If True (or the first tuple entry is True), then avoid coarsening
        diagonally dominant rows.  The second tuple entry requires a
        dictionary, where the key value 'theta' is used to tune the diagonal
        dominance threshold.
    keep : {bool} : default False
        Flag to indicate keeping extra operators in the hierarchy for
        diagnostics.  For example, if True, then strength of connection (C),
        tentative prolongation (T), and aggregation (AggOp) are kept.

    Other Parameters
    ----------------
    cycle_type : ['V','W','F']
        Structrure of multigrid cycle
    coarse_solver : ['splu', 'lu', 'cholesky, 'pinv', 'gauss_seidel', ... ]
        Solver used at the coarsest level of the MG hierarchy.
            Optionally, may be a tuple (fn, args), where fn is a string such as
        ['splu', 'lu', ...] or a callable function, and args is a dictionary of
        arguments to be passed to fn.

    Returns
    -------
    ml : multilevel_solver
        Multigrid hierarchy of matrices and prolongation operators

    See Also
    --------
    multilevel_solver, classical.ruge_stuben_solver,
    aggregation.smoothed_aggregation_solver

    Notes
    -----
        - This method implements classical-style SA, not root-node style SA
          (see aggregation.rootnode_solver).

        - The additional parameters are passed through as arguments to
          multilevel_solver.  Refer to pyamg.multilevel_solver for additional
          documentation.

        - At each level, four steps are executed in order to define the coarser
          level operator.

          1. Matrix A is given and used to derive a strength matrix, C.

          2. Based on the strength matrix, indices are grouped or aggregated.

          3. The aggregates define coarse nodes and a tentative prolongation
             operator T is defined by injection

          4. The tentative prolongation operator is smoothed by a relaxation
             scheme to improve the quality and extent of interpolation from the
             aggregates to fine nodes.

        - The parameters smooth, strength, aggregate, presmoother, postsmoother
          can be varied on a per level basis.  For different methods on
          different levels, use a list as input so that the i-th entry defines
          the method at the i-th level.  If there are more levels in the
          hierarchy than list entries, the last entry will define the method
          for all levels lower.

          Examples are:
          smooth=[('jacobi', {'omega':1.0}), None, 'jacobi']
          presmoother=[('block_gauss_seidel', {'sweep':symmetric}), 'sor']
          aggregate=['standard', 'naive']
          strength=[('symmetric', {'theta':0.25}), ('symmetric',
                                                    {'theta':0.08})]

        - Predefined strength of connection and aggregation schemes can be
          specified.  These options are best used together, but aggregation can
          be predefined while strength of connection is not.

          For predefined strength of connection, use a list consisting of
          tuples of the form ('predefined', {'C' : C0}), where C0 is a
          csr_matrix and each degree-of-freedom in C0 represents a supernode.
          For instance to predefine a three-level hierarchy, use
          [('predefined', {'C' : C0}), ('predefined', {'C' : C1}) ].

          Similarly for predefined aggregation, use a list of tuples.  For
          instance to predefine a three-level hierarchy, use [('predefined',
          {'AggOp' : Agg0}), ('predefined', {'AggOp' : Agg1}) ], where the
          dimensions of A, Agg0 and Agg1 are compatible, i.e.  Agg0.shape[1] ==
          A.shape[0] and Agg1.shape[1] == Agg0.shape[0].  Each AggOp is a
          csr_matrix.

    Examples
    --------
    >>> from pyamg import smoothed_aggregation_solver
    >>> from pyamg.gallery import poisson
    >>> from scipy.sparse.linalg import cg
    >>> import numpy
    >>> A = poisson((100,100), format='csr')           # matrix
    >>> b = numpy.ones((A.shape[0]))                   # RHS
    >>> ml = smoothed_aggregation_solver(A)            # AMG solver
    >>> M = ml.aspreconditioner(cycle='V')             # preconditioner
    >>> x,info = cg(A, b, tol=1e-8, maxiter=30, M=M)   # solve with CG

    References
    ----------
    .. [1] Vanek, P. and Mandel, J. and Brezina, M.,
       "Algebraic Multigrid by Smoothed Aggregation for
       Second and Fourth Order Elliptic Problems",
       Computing, vol. 56, no. 3, pp. 179--196, 1996.
       http://citeseer.ist.psu.edu/vanek96algebraic.html

    """

    if not (isspmatrix_csr(A) or isspmatrix_bsr(A)):
        try:
            A = csr_matrix(A)
            warn("Implicit conversion of A to CSR",
                 scipy.sparse.SparseEfficiencyWarning)
        except:
            raise TypeError('Argument A must have type csr_matrix or\
                             bsr_matrix, or be convertible to csr_matrix')

    A = A.asfptype()

    if (symmetry != 'symmetric') and (symmetry != 'hermitian') and\
            (symmetry != 'nonsymmetric'):
        raise ValueError('expected \'symmetric\', \'nonsymmetric\' or\
                         \'hermitian\' for the symmetry parameter ')
    A.symmetry = symmetry

    if A.shape[0] != A.shape[1]:
        raise ValueError('expected square matrix')
    ##
    # Right near nullspace candidates
    if B is None:
        B = np.ones((A.shape[0], 1), dtype=A.dtype)  # use constant vector
    else:
        B = np.asarray(B, dtype=A.dtype)

    ##
    # Left near nullspace candidates
    if A.symmetry == 'nonsymmetric':
        if BH is None:
            BH = B.copy()
        else:
            BH = np.asarray(BH, dtype=A.dtype)

    ##
    # Preprocess parameters
    max_levels, max_coarse, strength =\
        preprocess_str_or_agg(strength, max_levels, max_coarse)
    max_levels, max_coarse, aggregate =\
        preprocess_str_or_agg(aggregate, max_levels, max_coarse)
    improve_candidates = preprocess_improve_candidates(improve_candidates, A, max_levels)
    smooth = preprocess_smooth(smooth, max_levels)

    ##
    # Construct multilevel structure
    levels = []
    levels.append(multilevel_solver.level())
    levels[-1].A = A          # matrix

    ##
    # Append near nullspace candidates
    levels[-1].B = B          # right candidates
    if A.symmetry == 'nonsymmetric':
        levels[-1].BH = BH    # left candidates

    while len(levels) < max_levels and\
            levels[-1].A.shape[0]/blocksize(levels[-1].A) > max_coarse:
        extend_hierarchy(levels, strength, aggregate, smooth, improve_candidates, diagonal_dominance, keep)

    ml = multilevel_solver(levels, **kwargs)
    change_smoothers(ml, presmoother, postsmoother)
    return ml


def extend_hierarchy(levels, strength, aggregate, smooth, improve_candidates, 
                     diagonal_dominance=False, keep=True):
    """Service routine to implement the strength of connection, aggregation,
    tentative prolongation construction, and prolongation smoothing.  Called by
    smoothed_aggregation_solver.
    """

    def unpack_arg(v):
        if isinstance(v, tuple):
            return v[0], v[1]
        else:
            return v, {}

    A = levels[-1].A
    B = levels[-1].B
    if A.symmetry == "nonsymmetric":
        AH = A.H.asformat(A.format)
        BH = levels[-1].BH

    ##
    # Strength-of-Connection. Requirements for the strength matrix C are:
    #   * Nonzero diagonal whenever A has a nonzero diagonal
    #   * Non-negative entries (float or bool) in [0,1]
    #   * Large entries denoting stronger connections
    #   * C denotes nodal connections, i.e., if A is an nxn BSR matrix with 
    #     row block size of m, then C is (n/m) x (n/m) 
    fn, kwargs = unpack_arg(strength[len(levels)-1])
    if fn == 'symmetric':
        C = symmetric_strength_of_connection(A, **kwargs)
    elif fn == 'classical':
        C = classical_strength_of_connection(A, **kwargs)
    elif fn == 'distance':
        C = distance_strength_of_connection(A, **kwargs)
    elif (fn == 'ode') or (fn == 'evolution'):
        if kwargs.has_key('B'):
            C = evolution_strength_of_connection(A, **kwargs)
        else:
            C = evolution_strength_of_connection(A, B, **kwargs)
    elif fn == 'energy_based':
        C = energy_based_strength_of_connection(A, **kwargs)
    elif fn == 'predefined':
        C = kwargs['C'].tocsr()
    elif fn == 'algebraic_distance':
        C = algebraic_distance(A, **kwargs)
    elif fn is None:
        C = A.tocsr()
    else:
        raise ValueError('unrecognized strength of connection method: %s' %
                         str(fn))
    
    # Avoid coarsening diagonally dominant rows
    flag,kwargs = unpack_arg( diagonal_dominance )
    if flag:
        C = eliminate_diag_dom_nodes(A, C, **kwargs)

    ##
    # aggregation
    fn, kwargs = unpack_arg(aggregate[len(levels)-1])
    if fn == 'standard':
        AggOp = standard_aggregation(C, **kwargs)[0]
    elif fn == 'naive':
        AggOp = naive_aggregation(C, **kwargs)[0]
    elif fn == 'lloyd':
        AggOp = lloyd_aggregation(C, **kwargs)[0]
    elif fn == 'predefined':
        AggOp = kwargs['AggOp'].tocsr()
    else:
        raise ValueError('unrecognized aggregation method %s' % str(fn))

    ##
    # Improve near nullspace candidates (important to place after the call to
    # evolution_strength_of_connection)
    if improve_candidates[len(levels)-1] is not None:
        b = np.zeros((A.shape[0], 1), dtype=A.dtype)
        B = relaxation_as_linear_operator(improve_candidates[len(levels)-1], A, b) * B
        levels[-1].B = B
        if A.symmetry == "nonsymmetric":
            GOp = relaxation_as_linear_operator(improve_candidates[len(levels)-1], AH, b)
            BH = GOp * BH
            levels[-1].BH = BH

    ##
    # tentative prolongator
    T, B = fit_candidates(AggOp, B)
    if A.symmetry == "nonsymmetric":
        TH, BH = fit_candidates(AggOp, BH)

    ##
    # tentative prolongator smoother
    fn, kwargs = unpack_arg(smooth[len(levels)-1])
    if fn == 'jacobi':
        P = jacobi_prolongation_smoother(A, T, C, B, **kwargs)
    elif fn == 'richardson':
        P = richardson_prolongation_smoother(A, T, **kwargs)
    elif fn == 'energy':
        P = energy_prolongation_smoother(A, T, C, B, None, (False, {}),
                                         **kwargs)
    elif fn is None:
        P = T
    else:
        raise ValueError('unrecognized prolongation smoother method %s' %
                         str(fn))

    ##
    # Choice of R reflects A's structure
    symmetry = A.symmetry
    if symmetry == 'hermitian':
        R = P.H
    elif symmetry == 'symmetric':
        R = P.T
    elif symmetry == 'nonsymmetric':
        fn, kwargs = unpack_arg(smooth[len(levels)-1])
        if fn == 'jacobi':
            R = jacobi_prolongation_smoother(AH, TH, C, BH, **kwargs).H
        elif fn == 'richardson':
            R = richardson_prolongation_smoother(AH, TH, **kwargs).H
        elif fn == 'energy':
            R = energy_prolongation_smoother(AH, TH, C, BH, None, (False, {}),
                                             **kwargs)
            R = R.H
        elif fn is None:
            R = T.H
        else:
            raise ValueError('unrecognized prolongation smoother method %s' %
                             str(fn))

    if keep:
        levels[-1].C = C  # strength of connection matrix
        levels[-1].AggOp = AggOp  # aggregation operator
        levels[-1].T = T  # tentative prolongator

    levels[-1].P = P  # smoothed prolongator
    levels[-1].R = R  # restriction operator

    levels.append(multilevel_solver.level())
    A = R * A * P              # Galerkin operator
    A.symmetry = symmetry
    levels[-1].A = A
    levels[-1].B = B           # right near nullspace candidates

    if A.symmetry == "nonsymmetric":
        levels[-1].BH = BH     # left near nullspace candidates
