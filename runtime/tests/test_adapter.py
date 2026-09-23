"""Offline adapter qualification only; all credential-looking values are fake."""

from dataclasses import FrozenInstanceError, fields, replace
import itertools
import json
import unittest

from brainstem_agent.adapter import (
    AdapterLimits,
    CoreContractError,
    DEFAULT_LIMITS,
    build_core_request,
    normalize_core_response,
    normalize_sse,
)


def response(**changes):
    payload = {"response": "terminal answer", "agent_logs": [], "session_id": "s"}
    payload.update(changes)
    return payload


def event(name, payload, newline="\n"):
    return (
        f"event: {name}{newline}"
        f"data: {json.dumps(payload, ensure_ascii=False)}{newline}{newline}"
    )


def done(**changes):
    return event("done", response(**changes))


class LimitsTests(unittest.TestCase):
    def test_limits_are_frozen_and_defaults_are_positive(self):
        for field in fields(DEFAULT_LIMITS):
            self.assertGreater(getattr(DEFAULT_LIMITS, field.name), 0)
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_LIMITS.max_text_bytes = 1

    def test_each_limit_rejects_nonpositive_or_noninteger_values(self):
        for field in fields(DEFAULT_LIMITS):
            for value in (0, -1, True, False, 1.5, "3", None):
                with self.subTest(field=field.name, value=value):
                    with self.assertRaises(CoreContractError):
                        replace(DEFAULT_LIMITS, **{field.name: value})

    def test_all_entrypoints_validate_limits(self):
        for value in (None, {}, 1, True, "limits"):
            for call in (
                lambda: build_core_request("hi", "s", [], limits=value),
                lambda: normalize_core_response(response(), "s", limits=value),
                lambda: normalize_sse([done()], "s", limits=value),
            ):
                with self.subTest(value=value, call=call):
                    with self.assertRaises(CoreContractError):
                        call()

    def test_default_limits_refuse_oversized_input(self):
        oversized_text = "x" * (DEFAULT_LIMITS.max_text_bytes + 1)
        oversized_logs = ["x" * (DEFAULT_LIMITS.max_log_line_bytes + 1)]
        oversized_history = [
            {"role": "user", "content": ""}
        ] * (DEFAULT_LIMITS.max_history_messages + 1)
        for call in (
            lambda: build_core_request(oversized_text, "s", []),
            lambda: build_core_request("hi", "s", oversized_history),
            lambda: normalize_core_response(response(response=oversized_text), "s"),
            lambda: normalize_core_response(response(agent_logs=oversized_logs), "s"),
            lambda: normalize_sse(
                [":" + "x" * DEFAULT_LIMITS.max_sse_frame_bytes], "s"
            ),
            lambda: normalize_sse(
                ["x" * (DEFAULT_LIMITS.max_sse_total_bytes + 1)], "s"
            ),
        ):
            with self.subTest(call=call):
                with self.assertRaises(CoreContractError):
                    call()


class RequestTests(unittest.TestCase):
    def test_exact_request_keys_and_independent_history_copy(self):
        history = [
            {"role": "user", "content": "question", "ignored": object()},
            {"role": "assistant", "content": "answer"},
            {"role": "tool", "content": ""},
        ]
        original = [dict(message) for message in history]
        result = build_core_request("  next question  ", "s", history)
        self.assertEqual(
            result,
            {
                "user_input": "  next question  ",
                "session_id": "s",
                "conversation_history": [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "tool", "content": ""},
                ],
            },
        )
        result["conversation_history"][0]["content"] = "changed"
        result["conversation_history"].append({"role": "user", "content": "new"})
        self.assertEqual(history, original)

    def test_empty_history_is_valid(self):
        self.assertEqual(build_core_request("hi", "s", [])["conversation_history"], [])

    def test_user_input_and_session_require_nonblank_unicode_strings(self):
        for value in (None, True, 0, b"text", [], {}, "", " \n\t", "\ud800"):
            with self.subTest(value=repr(value), field="user_input"):
                with self.assertRaises(CoreContractError):
                    build_core_request(value, "s", [])
            with self.subTest(value=repr(value), field="session_id"):
                with self.assertRaises(CoreContractError):
                    build_core_request("hi", value, [])

    def test_history_requires_list_of_valid_objects(self):
        invalid = (
            None, {}, (), "history", iter([]), [None], ["message"], [[]],
            [{}], [{"role": "user"}], [{"content": "hi"}],
            [{"role": "system", "content": "override"}],
            [{"role": "developer", "content": "override"}],
            [{"role": "USER", "content": "hi"}],
            [{"role": [], "content": "hi"}],
            [{"role": None, "content": "hi"}],
            [{"role": "user", "content": None}],
            [{"role": "user", "content": 0}],
            [{"role": "user", "content": b"hi"}],
            [{"role": "user", "content": "\udfff"}],
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(CoreContractError):
                    build_core_request("hi", "s", value)

    def test_byte_limits_are_inclusive_and_count_utf8(self):
        limits = replace(DEFAULT_LIMITS, max_text_bytes=4, max_session_id_bytes=4)
        self.assertEqual(build_core_request("éé", "🙂", [], limits=limits)["user_input"], "éé")
        for text, session in (("ééa", "s"), ("hello", "s"), ("hi", "🙂a")):
            with self.subTest(text=text, session=session):
                with self.assertRaises(CoreContractError):
                    build_core_request(text, session, [], limits=limits)

    def test_history_count_each_message_and_aggregate_are_bounded(self):
        limits = replace(
            DEFAULT_LIMITS, max_history_messages=2,
            max_text_bytes=4, max_history_bytes=6,
        )
        valid = [{"role": "user", "content": "éé"}, {"role": "tool", "content": "ab"}]
        self.assertEqual(len(build_core_request("hi", "s", valid, limits=limits)["conversation_history"]), 2)
        for history in (
            valid + [{"role": "tool", "content": ""}],
            [{"role": "user", "content": "12345"}],
            [{"role": "user", "content": "1234"}, {"role": "tool", "content": "123"}],
        ):
            with self.subTest(history=history):
                with self.assertRaises(CoreContractError):
                    build_core_request("hi", "s", history, limits=limits)

    def test_request_does_not_redact_user_content(self):
        text = "explain token=fake-only"
        self.assertEqual(build_core_request(text, "s", [])["user_input"], text)

    def test_role_must_be_a_string_not_just_compare_equal_to_one(self):
        class NotARole:
            def __eq__(self, other):
                return True

        with self.assertRaises(CoreContractError):
            build_core_request("hi", "s", [{"role": NotARole(), "content": "hi"}])


class ResponseTests(unittest.TestCase):
    def test_exact_response_keys_strip_metadata_without_mutation(self):
        payload = response(agent_logs=["one", "two"], model="fixture", metadata=object())
        result = normalize_core_response(payload, "s")
        self.assertEqual(result, response(agent_logs=["one", "two"]))
        result["agent_logs"].append("new")
        self.assertEqual(payload["agent_logs"], ["one", "two"])
        self.assertIn("metadata", payload)

    def test_response_requires_object_and_all_fields(self):
        for payload in (None, [], "answer", 1, True, b"{}", ()):
            with self.subTest(payload=payload):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(payload, "s")
        for key in response():
            payload = response()
            del payload[key]
            with self.subTest(missing=key):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(payload, "s")

    def test_malformed_required_fields_refuse(self):
        bad_fields = {
            "response": (None, False, 7, {}, [], b"answer", "", " \t\r\n", "\ud800"),
            "agent_logs": (None, False, 7, {}, (), [1], ["okay", None], [b"log"], "\ud800"),
            "session_id": (None, False, 7, {}, [], b"s", "", " ", "\ud800"),
        }
        for key, values in bad_fields.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)):
                    with self.assertRaises(CoreContractError):
                        normalize_core_response(response(**{key: value}), "s")

    def test_expected_session_is_validated_and_must_match_exactly(self):
        for expected in (None, False, 0, [], {}, b"s", "", " ", "\ud800", "S", "s "):
            with self.subTest(expected=repr(expected)):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(response(), expected)

    def test_error_shaped_http_200_never_becomes_success(self):
        markers = (
            {"error": "no_copilot_access"}, {"error": None}, {"errors": []},
            {"error_code": None}, {"no_copilot_access": True},
            {"success": False}, {"ok": False}, {"status": "error"},
            {"status": "FAILED"}, {"status": "denied"}, {"type": "error"},
            {"code": "no_copilot_access"}, {"status_code": 403},
        )
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(response(**marker), "s")
        with self.assertRaises(CoreContractError):
            normalize_core_response({"error": "no_copilot_access"}, "s")

    def test_success_metadata_is_stripped_not_interpreted_as_an_error(self):
        payload = response(
            response="A discussion of no_copilot_access errors",
            status="completed", status_code=200, success=True, ok=True, code="ok",
        )
        self.assertEqual(
            normalize_core_response(payload, "s"),
            response(response="A discussion of no_copilot_access errors"),
        )

    def test_unknown_metadata_is_not_traversed_or_retained(self):
        cycle = {}
        cycle["itself"] = cycle
        payload = response(
            extra=cycle, code="unrecognized" * 10000, status=["opaque", object()]
        )
        self.assertEqual(normalize_core_response(payload, "s"), response())

    def test_flat_logs_split_deterministically(self):
        for logs, expected in (
            ("", []), ("one", ["one"]), ("one\n", ["one"]),
            ("\n", [""]), ("one\r\ntwo\rthree\n\nfour\u2028five",
                           ["one", "two", "three", "", "four", "five"]),
        ):
            with self.subTest(logs=repr(logs)):
                self.assertEqual(
                    normalize_core_response(response(agent_logs=logs), "s")["agent_logs"],
                    expected,
                )

    def test_list_logs_remain_entries_even_with_embedded_newlines(self):
        logs = ["one\ntwo", "", "three\r\nfour"]
        result = normalize_core_response(response(agent_logs=logs), "s")
        self.assertEqual(result["agent_logs"], logs)
        self.assertIsNot(result["agent_logs"], logs)

    def test_redacts_known_assignments_and_bearer_values(self):
        cases = {
            "api_key=fake-alpha": "api_key=[REDACTED]",
            "OPENAI_API_KEY = fake-alpha; step=okay": "OPENAI_API_KEY = [REDACTED]; step=okay",
            '"access_token": "fake alpha"': '"access_token": "[REDACTED]"',
            "'client-secret' : 'fake beta'": "'client-secret' : '[REDACTED]'",
            "GITHUB_TOKEN=fake-gamma": "GITHUB_TOKEN=[REDACTED]",
            "AWS_SECRET_ACCESS_KEY=fake-delta": "AWS_SECRET_ACCESS_KEY=[REDACTED]",
            "aws_access_key_id=fake-epsilon": "aws_access_key_id=[REDACTED]",
            'password="fake \\"quoted\\" value"': 'password="[REDACTED]"',
            "Authorization: Bearer fake.segment/plus+==": "Authorization: Bearer [REDACTED]",
            'header="bEaReR fake-value"': 'header="bEaReR [REDACTED]"',
            "token=Bearer fake-value": "token=[REDACTED] [REDACTED]",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                normalized = normalize_core_response(
                    response(response=source, agent_logs=[source]), "s"
                )
                self.assertEqual(normalized["response"], expected)
                self.assertEqual(normalized["agent_logs"], [expected])

    def test_redaction_limitations_are_explicit_not_universal(self):
        unlabelled = "fake-unlabelled-value and encoded ZmFrZS1vbmx5"
        self.assertEqual(
            normalize_core_response(response(agent_logs=[unlabelled]), "s")["agent_logs"],
            [unlabelled],
        )
        ordinary = "step=complete tokens=12 secretive=okay tokenizer=fast"
        self.assertEqual(normalize_core_response(response(response=ordinary), "s")["response"], ordinary)

    def test_normalization_is_idempotent(self):
        payload = response(
            response='token="fake-only"',
            agent_logs="Authorization: Bearer fake-only\napi_key=fake-only\n",
            unused="metadata",
        )
        normalized = normalize_core_response(payload, "s")
        self.assertEqual(normalize_core_response(normalized, "s"), normalized)

    def test_response_and_session_byte_boundaries(self):
        limits = replace(DEFAULT_LIMITS, max_text_bytes=4, max_session_id_bytes=4)
        self.assertEqual(
            normalize_core_response(response(response="éé", session_id="🙂"), "🙂", limits=limits),
            response(response="éé", session_id="🙂"),
        )
        for payload, expected in (
            (response(response="ééa"), "s"),
            (response(response="okay", session_id="🙂a"), "🙂a"),
            (response(response="okay"), "🙂a"),
        ):
            with self.subTest(payload=payload, expected=expected):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(payload, expected, limits=limits)

    def test_logs_bound_count_line_bytes_and_aggregate_bytes(self):
        limits = replace(
            DEFAULT_LIMITS, max_log_lines=2, max_log_line_bytes=4, max_log_bytes=6
        )
        self.assertEqual(normalize_core_response(response(agent_logs=["éé", "ab"]), "s", limits=limits)["agent_logs"], ["éé", "ab"])
        for logs in (["", "", ""], "\n\n\n", ["ééa"], "12345",
                     ["1234", "123"], "123\n123"):
            with self.subTest(logs=logs):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(response(agent_logs=logs), "s", limits=limits)

    def test_redaction_cannot_expand_beyond_output_bounds(self):
        for limits, payload in (
            (replace(DEFAULT_LIMITS, max_text_bytes=10), response(response="token=x")),
            (replace(DEFAULT_LIMITS, max_log_line_bytes=10), response(agent_logs=["token=x"])),
            (replace(DEFAULT_LIMITS, max_log_bytes=20), response(agent_logs=["token=x", "token=y"])),
        ):
            with self.subTest(limits=limits):
                with self.assertRaises(CoreContractError):
                    normalize_core_response(payload, "s", limits=limits)

    def test_errors_do_not_echo_payload_or_identifiers(self):
        fake = "fake-sensitive-value"
        cases = (
            lambda: normalize_core_response(response(session_id=fake), "s"),
            lambda: normalize_core_response(response(error=fake), "s"),
            lambda: normalize_core_response(response(agent_logs=[{"secret": fake}]), "s"),
        )
        for call in cases:
            with self.assertRaises(CoreContractError) as caught:
                call()
            self.assertNotIn(fake, str(caught.exception))


class SSETests(unittest.TestCase):
    def test_done_response_is_authoritative_not_accumulated_deltas(self):
        stream = (
            event("delta", {"delta": "not the answer", "session_id": "s"})
            + event("delta", {"delta": " nor this"})
            + done(response="actual final", agent_logs="one\ntwo", ignored=True)
        )
        self.assertEqual(
            normalize_sse([stream], "s"),
            response(response="actual final", agent_logs=["one", "two"]),
        )

    def test_every_split_boundary_including_crlf_and_unicode(self):
        stream = (
            ": heartbeat\r\n\r\n"
            + event("delta", {"delta": "provisional 🙂"}, "\r\n")
            + event("done", response(response="résumé 🙂"), "\r\n")
            + ": trailing\r\n\r\n"
        )
        expected = response(response="résumé 🙂")
        for boundary in range(len(stream) + 1):
            with self.subTest(boundary=boundary):
                self.assertEqual(normalize_sse([stream[:boundary], stream[boundary:]], "s"), expected)
        self.assertEqual(normalize_sse(iter(stream), "s"), expected)

    def test_bare_cr_and_mixed_line_endings(self):
        for newline in ("\n", "\r", "\r\n"):
            with self.subTest(newline=repr(newline)):
                self.assertEqual(
                    normalize_sse([event("done", response(), newline)], "s"), response()
                )
        mixed = "event: done\rdata: " + json.dumps(response()) + "\r\n\n"
        self.assertEqual(normalize_sse([mixed], "s"), response())

    def test_multiline_data_comments_id_and_retry(self):
        stream = (
            "id: fixture-only\nretry: 1000\n: ignored\n"
            "event: done\n"
            'data: {"response": "answer",\n'
            ': comment between data lines\n'
            'data: "agent_logs": [], "session_id": "s"}\n\n'
        )
        self.assertEqual(normalize_sse([stream], "s"), response(response="answer"))

    def test_data_only_and_default_message_type_discriminators(self):
        for prefix in ("", "event:\n", "event: message\n"):
            with self.subTest(prefix=prefix):
                stream = prefix + "data: " + json.dumps(response(type="done")) + "\n\n"
                self.assertEqual(normalize_sse([stream], "s"), response())
        stream = 'data: {"type":"delta","delta":"discarded"}\n\n'
        stream += "data: " + json.dumps(response(type="done")) + "\n\n"
        self.assertEqual(normalize_sse([stream], "s"), response())

    def test_data_only_discriminator_must_be_a_supported_string(self):
        for value in (None, 1, True, [], {}, "unknown", "DONE"):
            with self.subTest(value=value):
                stream = "data: " + json.dumps(response(type=value)) + "\n\n"
                with self.assertRaises(CoreContractError):
                    normalize_sse([stream], "s")

    def test_named_event_and_payload_type_must_agree(self):
        for name, payload in (
            ("delta", {"type": "done"}), ("done", response(type="delta")),
            ("done", response(type=None)), ("done", response(type=[])),
        ):
            with self.subTest(name=name, payload=payload):
                with self.assertRaises(CoreContractError):
                    normalize_sse([event(name, payload)], "s")
        self.assertEqual(normalize_sse([event("done", response(type="done"))], "s"), response())

    def test_eof_without_complete_done_refuses(self):
        for stream in (
            "", "\n\n", ": comment\n\n", event("delta", {"delta": "tempting answer"}),
            done().rstrip("\n"), done()[:-1], "event: done\n\n",
        ):
            with self.subTest(stream=stream):
                with self.assertRaises(CoreContractError):
                    normalize_sse([stream], "s")

    def test_error_after_delta_is_not_partial_success(self):
        for error in (
            event("error", {"error": "fixture refusal"}), "event: error\n\n",
            'data: {"type":"error","error":"no_copilot_access"}\n\n',
            event("delta", {"error": "no_copilot_access"}),
            done(error="no_copilot_access"),
        ):
            with self.subTest(error=error):
                with self.assertRaises(CoreContractError):
                    normalize_sse([event("delta", {"delta": "partial"}), error], "s")

    def test_wrong_or_malformed_session_on_delta_or_done_refuses(self):
        for value in ("other", "", " ", None, 1, [], {}, "\ud800"):
            for name in ("delta", "done"):
                payload = response(session_id=value) if name == "done" else {"delta": "x", "session_id": value}
                # ASCII JSON keeps the intentionally invalid surrogate in JSON,
                # instead of constructing an invalid Unicode text chunk.
                frame = f"event: {name}\ndata: {json.dumps(payload)}\n\n"
                with self.subTest(name=name, value=repr(value)):
                    with self.assertRaises(CoreContractError):
                        normalize_sse([frame, done()], "s")

    def test_terminal_fields_receive_full_response_validation(self):
        payloads = []
        for key in response():
            value = response()
            del value[key]
            payloads.append(value)
        payloads += [response(response=""), response(agent_logs=[None]), response(response={})]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(CoreContractError):
                    normalize_sse([event("done", payload)], "s")

    def test_duplicate_json_members_refuse_including_nested_metadata(self):
        samples = (
            '{"response":"first","response":"second","agent_logs":[],"session_id":"s"}',
            '{"response":"answer","agent_logs":[],"session_id":"s","session_id":"s"}',
            '{"response":"answer","agent_logs":[],"session_id":"s","extra":{"a":1,"a":2}}',
            '{"delta":"first","delta":"second"}',
            '{"delta":"x","extra":[{"a":1,"\\u0061":2}]}',
        )
        for data in samples:
            with self.subTest(data=data):
                with self.assertRaisesRegex(CoreContractError, "duplicate"):
                    normalize_sse(["event: delta\ndata: " + data + "\n\n", done()], "s")

    def test_nonfinite_json_refuses_even_in_ignored_metadata(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            for name in ("delta", "done"):
                data = json.dumps(response())[:-1] + f', "extra": {value}' + "}"
                with self.subTest(value=value, name=name):
                    with self.assertRaisesRegex(CoreContractError, "non-finite"):
                        normalize_sse([f"event: {name}\ndata: {data}\n\n", done()], "s")

    def test_malformed_json_and_nonobject_data_refuse(self):
        for data in (
            "", "{", '{"a":}', '{"a":1,}', '{"a":1} garbage', '{"a":"\x00"}',
            "null", "true", "1", '""', "[]", "{}", "\ufeff{}",
        ):
            with self.subTest(data=repr(data)):
                with self.assertRaises(CoreContractError):
                    normalize_sse([f"event: done\ndata: {data}\n\n"], "s")

    def test_duplicate_done_and_all_trailing_events_refuse(self):
        for trailing in (
            done(), event("delta", {"delta": "late"}), event("error", {"error": "late"}),
            "event: unknown\n\n", "data: {malformed}\n\n", "data: {",
            "id: late\n\n", "retry: 1\n\n", "unknown: field\n\n", "junk",
            ": unfinished",
        ):
            with self.subTest(trailing=trailing):
                with self.assertRaises(CoreContractError):
                    normalize_sse([done(), trailing], "s")

    def test_after_done_only_terminated_comments_and_blank_frames_are_allowed(self):
        stream = done() + "\n: comment\n\n\r\n: another\r\n\r\n"
        self.assertEqual(normalize_sse([stream], "s"), response())
        for ending in ("\n", "\r", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.assertEqual(
                    normalize_sse([done(), ": complete comment" + ending], "s"),
                    response(),
                )

    def test_malformed_or_unknown_fields_and_event_types_refuse(self):
        for stream in (
            "garbage\n\n", "unknown: field\n\n", "event: mystery\ndata: {}\n\n",
            "event: Done\ndata: {}\n\n", "event: delta\nevent: done\ndata: {}\n\n",
            "data: {}\n\n", 'data: {"type":[]}\n\n', "event: done \ndata: {}\n\n",
            "event: done\n data: {}\n\n",
        ):
            with self.subTest(stream=stream):
                with self.assertRaises(CoreContractError):
                    normalize_sse([stream, done()], "s")

    def test_argument_validation_for_chunks_and_expected_session(self):
        for chunks in (None, 0, False, done(), done().encode(), bytearray(b"data"), {}):
            with self.subTest(chunks=repr(chunks)):
                with self.assertRaises(CoreContractError):
                    normalize_sse(chunks, "s")
        for chunk in (None, 1, False, b"data", {}, [], "\ud800"):
            with self.subTest(chunk=repr(chunk)):
                with self.assertRaises(CoreContractError):
                    normalize_sse([chunk], "s")
        for expected in (None, [], "", " ", "\ud800", "s" * 257):
            with self.subTest(expected=repr(expected)):
                with self.assertRaises(CoreContractError):
                    normalize_sse([done()], expected)

    def test_invalid_expected_session_does_not_consume_source(self):
        consumed = []

        def source():
            consumed.append(True)
            yield done()

        with self.assertRaises(CoreContractError):
            normalize_sse(source(), "")
        self.assertEqual(consumed, [])

    def test_iterator_errors_refuse_even_after_done_without_echoing_details(self):
        class CannotIterate:
            def __iter__(self):
                raise RuntimeError("fake-sensitive-detail")

        def interrupted(after_done):
            yield done() if after_done else event("delta", {"delta": "partial"})
            raise RuntimeError("fake-sensitive-detail")

        for source in (CannotIterate(), interrupted(False), interrupted(True)):
            with self.subTest(source=source):
                with self.assertRaises(CoreContractError) as caught:
                    normalize_sse(source, "s")
                self.assertNotIn("fake-sensitive-detail", str(caught.exception))

    def test_frame_byte_limit_includes_comments_and_crlf(self):
        frame = event("done", response(response="🙂"), "\r\n")
        size = len(frame.encode("utf-8"))
        limits = replace(DEFAULT_LIMITS, max_sse_frame_bytes=size)
        self.assertEqual(normalize_sse(list(frame), "s", limits=limits), response(response="🙂"))
        with self.assertRaises(CoreContractError):
            normalize_sse([frame], "s", limits=replace(limits, max_sse_frame_bytes=size - 1))
        with self.assertRaises(CoreContractError):
            normalize_sse([":" + "x" * size, done()], "s", limits=limits)

    def test_total_byte_limit_counts_all_frames_and_trailing_comments(self):
        stream = done() + ": 🙂\n\n"
        size = len(stream.encode("utf-8"))
        limits = replace(DEFAULT_LIMITS, max_sse_total_bytes=size)
        self.assertEqual(normalize_sse([stream, ""], "s", limits=limits), response())
        with self.assertRaises(CoreContractError):
            normalize_sse([stream, "\n"], "s", limits=limits)
        with self.assertRaises(CoreContractError):
            normalize_sse([stream], "s", limits=replace(limits, max_sse_total_bytes=size - 1))

    def test_chunk_limit_bounds_empty_chunk_floods(self):
        limits = replace(DEFAULT_LIMITS, max_sse_chunks=3)
        self.assertEqual(normalize_sse(["", done(), ""], "s", limits=limits), response())
        for chunks in (["", done(), "", ""], itertools.repeat(""), itertools.chain([done()], itertools.repeat(""))):
            with self.subTest(chunks=chunks):
                with self.assertRaisesRegex(CoreContractError, "chunk limit"):
                    normalize_sse(chunks, "s", limits=limits)

    def test_frame_count_bounds_empty_and_comment_frame_floods(self):
        limits = replace(DEFAULT_LIMITS, max_sse_frames=2)
        self.assertEqual(normalize_sse(["\n" + done()], "s", limits=limits), response())
        for stream in ("\n\n" + done(), done() + ": one\n\n: two\n\n"):
            with self.subTest(stream=stream):
                with self.assertRaisesRegex(CoreContractError, "frame limit"):
                    normalize_sse([stream], "s", limits=limits)

    def test_json_depth_limit_ignores_braces_and_escapes_in_strings(self):
        limits = replace(DEFAULT_LIMITS, max_json_depth=2)
        payload = response(response='literal [[[{{{\\"', extra={"okay": 1})
        self.assertEqual(normalize_sse([event("done", payload)], "s", limits=limits)["response"], payload["response"])
        for extra in ({"too": {"deep": True}}, [[[1]]]):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(CoreContractError, "nesting limit"):
                    normalize_sse([done(extra=extra)], "s", limits=limits)
        deep = "event: delta\ndata: " + "[" * 2000 + "]" * 2000 + "\n\n"
        with self.assertRaises(CoreContractError):
            normalize_sse([deep], "s")

    def test_json_number_lexemes_are_bounded_without_changing_global_settings(self):
        limits = replace(DEFAULT_LIMITS, max_json_number_chars=3)
        for value in ("123", "1.5"):
            data = json.dumps(response())[:-1] + f', "extra": {value}' + "}"
            self.assertEqual(normalize_sse([f"event: done\ndata: {data}\n\n"], "s", limits=limits), response())
        for value in ("1234", "1.25", "1e10", "9" * 5000):
            data = f"event: delta\ndata: {{\"extra\":{value}}}\n\n"
            with self.subTest(value=value[:20]):
                with self.assertRaisesRegex(CoreContractError, "number limit"):
                    normalize_sse([data, done()], "s", limits=limits)

    def test_terminal_normalization_applies_redaction_and_text_bounds(self):
        self.assertEqual(
            normalize_sse([done(response="token=fake-only", agent_logs="Bearer fake-only")], "s"),
            response(response="token=[REDACTED]", agent_logs=["Bearer [REDACTED]"]),
        )
        with self.assertRaises(CoreContractError):
            normalize_sse([done()], "s", limits=replace(DEFAULT_LIMITS, max_text_bytes=3))

    def test_source_is_consumed_once_without_redispatch(self):
        consumed = []

        def source():
            for frame in (event("delta", {"delta": "partial"}), done(), ": end\n\n"):
                consumed.append(frame)
                yield frame

        self.assertEqual(normalize_sse(source(), "s"), response())
        self.assertEqual(len(consumed), 3)


if __name__ == "__main__":
    unittest.main()
