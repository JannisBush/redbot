from redbot.message import headers
from redbot.speak import Note, categories, levels
from redbot.syntax import rfc7230
from redbot.type import AddNoteMethodType


class transfer_encoding(headers.HttpHeader):
    canonical_name = "Transfer-Encoding"
    description = """\
The `Transfer-Encoding` header indicates what (if any) type of transformation has been applied to
the message content.

This differs from `Content-Encoding` in that transfer-codings are a property of the message, not of
the representation; i.e., it will be removed by the next "hop", whereas content-codings are
end-to-end.

The most commonly used transfer-coding is `chunked`, which allows persistent connections to be used
without knowing the content's length."""
    reference = f"{rfc7230.SPEC_URL}#header.transfer-encoding"
    syntax = rfc7230.Transfer_Encoding
    list_header = True
    deprecated = False
    valid_in_requests = True
    valid_in_responses = True

    def parse(self, field_value: str, add_note: AddNoteMethodType) -> str:
        try:
            coding, param_str = field_value.split(";", 1)
        except ValueError:
            coding, param_str = field_value, ""
        coding = coding.lower()
        param_dict = headers.parse_params(param_str, add_note, True)
        if param_dict:
            add_note(TRANSFER_CODING_PARAM)
        return coding

    def evaluate(self, add_note: AddNoteMethodType) -> None:
        unwanted = {c for c in self.value if c not in ["chunked", "identity"]}
        if unwanted:
            add_note(TRANSFER_CODING_UNWANTED, unwanted_codings=", ".join(unwanted))
        if "identity" in self.value:
            add_note(TRANSFER_CODING_IDENTITY)


class TRANSFER_CODING_IDENTITY(Note):
    category = categories.CONNECTION
    level = levels.INFO
    summary = "The identity transfer-coding isn't necessary."
    text = """\
HTTP defines _transfer-codings_ as a hop-by-hop encoding of the message content. The `identity`
tranfer-coding was defined as the absence of encoding; it doesn't do anything, so it's necessary.

You can remove this token to save a few bytes."""


class TRANSFER_CODING_UNWANTED(Note):
    category = categories.CONNECTION
    level = levels.BAD
    summary = "%(response)s has unsupported transfer-coding."
    text = """\
%(response)s's `Transfer-Encoding` header indicates it has transfer-codings applied, but REDbot didn't
ask for it (or them) to be.

They are: `%(unwanted_codings)s`

Normally, clients ask for the encodings they want in the `TE` request header. Using codings that
the client doesn't explicitly request can lead to interoperability problems."""


class TRANSFER_CODING_PARAM(Note):
    category = categories.CONNECTION
    level = levels.WARN
    summary = "%(response)s had parameters on its transfer-codings."
    text = """\
HTTP allows transfer-codings in the `Transfer-Encoding` header to have optional parameters, but it
doesn't define what they mean.

%(response)s has encodings with such parameters; although they're technically allowed, they may
cause interoperability problems. They should be removed."""


class TransferEncodingTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chunked"]
    expected_out = ["chunked"]


class TransferEncodingParamTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chunked; foo=bar"]
    expected_out = ["chunked"]
    expected_err = [TRANSFER_CODING_PARAM]


class BadTransferEncodingTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chunked=foo"]
    expected_out = ["chunked=foo"]
    expected_err = [headers.BAD_SYNTAX, TRANSFER_CODING_UNWANTED]


class TransferEncodingCaseTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chUNked"]
    expected_out = ["chunked"]


class TransferEncodingIdentityTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"identity"]
    expected_out = ["identity"]
    expected_err = [TRANSFER_CODING_IDENTITY]


class TransferEncodingUnwantedTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"foo"]
    expected_out = ["foo"]
    expected_err = [TRANSFER_CODING_UNWANTED]


class TransferEncodingMultTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chunked", b"identity"]
    expected_out = ["chunked", "identity"]
    expected_err = [TRANSFER_CODING_IDENTITY]


class TransferEncodingMultUnwantedTest(headers.HeaderTest):
    name = "Transfer-Encoding"
    inputs = [b"chunked", b"foo", b"bar"]
    expected_out = ["chunked", "foo", "bar"]
    expected_err = [TRANSFER_CODING_UNWANTED]
