"""Public citation and Taiwan drug lookup. No account and no patient data.

``lookup_pubmed`` talks to NCBI E-utilities. ``lookup_europepmc`` talks to the
Europe PMC REST search. ``lookup_tfda`` and ``lookup_nhi`` read the open-data
files data.gov.tw publishes for drug licences and NHI prices. Pass ``fetch`` to
any of them and nothing is downloaded.
"""

from wardmuse.cite.drugs import lookup_nhi, lookup_tfda
from wardmuse.cite.europepmc import lookup_europepmc
from wardmuse.cite.http import CiteError
from wardmuse.cite.pubmed import lookup_pubmed

__all__ = [
    "CiteError",
    "lookup_europepmc",
    "lookup_nhi",
    "lookup_pubmed",
    "lookup_tfda",
]
