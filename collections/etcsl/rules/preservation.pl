:- module(preservation, [
    required_representation/3,
    missing_representation/4
], [assertions, regtypes]).

:- doc(title, "ETCSL preservation completeness rules").
:- doc(author, "text-preserver contributors").
:- doc(module, "Defines which ETCSL representations are required and reports
   missing representations without treating the intentionally untranslated
   category-0 compositions as incomplete.").

:- regtype composition_id/1.
composition_id(Id) :- atm(Id).

:- regtype composition_ids/1.
composition_ids([]).
composition_ids([Id|Ids]) :-
    composition_id(Id),
    composition_ids(Ids).

:- regtype representation_kind/1.
representation_kind(transliteration).
representation_kind(translation).

:- regtype representation_kinds/1.
representation_kinds([]).
representation_kinds([Kind|Kinds]) :-
    representation_kind(Kind),
    representation_kinds(Kinds).

:- regtype atom_list/1.
atom_list([]).
atom_list([X|Xs]) :-
    atm(X),
    atom_list(Xs).

:- calls contains(Atom, Atoms)
   : (atm(Atom), atom_list(Atoms)).
contains(X, [X|_]).
contains(X, [_|Xs]) :-
    contains(X, Xs).

:- pred required_representation(Composition, KnownUntranslated, Kind)
   : (composition_id(Composition), composition_ids(KnownUntranslated))
   => representation_kind(Kind)
   # "@var{Kind} is required for @var{Composition}.".
required_representation(_, _, transliteration).
required_representation(Composition, KnownUntranslated, translation) :-
    \+ contains(Composition, KnownUntranslated).

:- pred missing_representation(Composition, KnownUntranslated,
                               CapturedKinds, Kind)
   : (composition_id(Composition), composition_ids(KnownUntranslated),
      representation_kinds(CapturedKinds))
   => representation_kind(Kind)
   # "@var{Kind} is required but absent from @var{CapturedKinds}.".
missing_representation(Composition, KnownUntranslated, CapturedKinds, Kind) :-
    required_representation(Composition, KnownUntranslated, Kind),
    \+ contains(Kind, CapturedKinds).
