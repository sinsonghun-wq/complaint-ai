"""Keep the C CSV parser on the normal path; recover oversized records safely."""
import csv
import itertools
import re

CSV_MAX_FIELD_CHARS = 2_000_000
csv.field_size_limit(CSV_MAX_FIELD_CHARS)
_DELIMITERS = re.compile('[",\\r\\n]')


class CsvRowError(dict):
    """Small failure metadata, never a truncated complaint or huge raw field."""
    def __init__(self, start_line, end_line):
        super().__init__(_csv_read_error={"kind": "field_too_long", "limit": CSV_MAX_FIELD_CHARS,
                                         "line_start": start_line, "line_end": end_line})

    @property
    def reason(self):
        detail = self["_csv_read_error"]
        return (f"CSV {detail['line_start']}~{detail['line_end']}행을 읽을 수 없습니다. "
                f"필드 길이가 {detail['limit']:,}자를 초과하여 해당 항목을 제외했습니다.")


class _RecordLines:
    def __init__(self, source):
        self.source = iter(source)
        self.line_num = 0
        self.captured = []
        self.capture = True

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.source)
        self.line_num += 1
        if self.capture:
            self.captured.append(line)
        return line

    def discard_record(self):
        # csv.reader has consumed the entire physical line on which it failed.
        # Replay only that record's lines to find its true end, including quoted
        # newlines and escaped quotes; do not mistake a body line for a new row.
        self.capture = False
        state = "start"
        try:
            for line in itertools.chain(self.captured, self):
                previous = 0
                for match in _DELIMITERS.finditer(line):
                    if match.start() > previous and state in ("start", "closed"):
                        state = "plain"
                    character = match.group()
                    if character == '"':
                        if state == "start": state = "quoted"
                        elif state == "quoted": state = "closed"
                        elif state == "closed": state = "quoted"
                    elif character == ",":
                        if state != "quoted": state = "start"
                    elif state != "quoted":
                        return
                    previous = match.end()
                if previous < len(line) and state in ("start", "closed"):
                    state = "plain"
                if state != "quoted":
                    return
        finally:
            self.captured.clear()
            self.capture = True


class CsvRecordReader:
    def __init__(self, source):
        csv.field_size_limit(CSV_MAX_FIELD_CHARS)
        self.lines = _RecordLines(source)
        self.reader = csv.reader(self.lines)
        try:
            self.fieldnames = [header.strip() for header in next(self.reader)]
        except StopIteration:
            self.fieldnames = []
        except csv.Error as error:
            raise csv.Error(f"CSV 헤더를 읽을 수 없습니다. ({error})") from error
        self.lines.captured.clear()

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            self.lines.captured.clear()
            start_line = self.lines.line_num + 1
            try:
                values = next(self.reader)
            except csv.Error as error:
                if "field larger than field limit" not in str(error):
                    raise
                self.lines.discard_record()
                return CsvRowError(start_line, self.lines.line_num)
            if not values:  # Match DictReader's blank-line handling.
                continue
            row = dict(zip(self.fieldnames, values))
            if len(values) > len(self.fieldnames):
                row[None] = values[len(self.fieldnames):]
            elif len(values) < len(self.fieldnames):
                row.update((header, None) for header in self.fieldnames[len(values):])
            self.lines.captured.clear()
            return row
