#!/usr/bin/env python
"""An implementation of an in-memory data store for testing."""


import sys
import threading
import time

from grr.lib import config_lib
from grr.lib import data_store
from grr.lib import rdfvalue
from grr.lib import utils


class FakeTransaction(data_store.CommonTransaction):
  """A fake transaction object for testing."""

  def __init__(self, store, subject, lease_time=None, token=None):
    subject = utils.SmartUnicode(subject)
    super(FakeTransaction, self).__init__(
        store, subject, lease_time=lease_time, token=token)
    self.locked = False
    if lease_time is None:
      lease_time = config_lib.CONFIG["Datastore.transaction_timeout"]

    self.expires = time.time() + lease_time

    with self.store.lock:
      expires = store.transactions.get(subject)
      if expires and time.time() < expires:
        raise data_store.TransactionError("Subject is locked")

      # Check expiry time.
      store.transactions[subject] = self.expires

      self.locked = True

  def CheckLease(self):
    return max(0, self.expires - time.time())

  def UpdateLease(self, duration):
    self.expires = time.time() + duration
    self.store.transactions[self.subject] = self.expires

  def Abort(self):
    self.Unlock()

  def Commit(self):
    # Locking here ensures that datastore reads in the middle of the
    # transaction are not possible. This prevents nasty race conditions
    # in Selenium tests where 2 threads (test thread and AdminUI server thread)
    # may access the datastore at the same time.
    with self.store.lock:
      super(FakeTransaction, self).Commit()

    self.Unlock()

  def Unlock(self):
    with self.store.lock:
      if self.locked:
        self.store.transactions.pop(self.subject, None)
        self.locked = False


class FakeDataStore(data_store.DataStore):
  """A fake data store - Everything is in memory."""

  def __init__(self):
    super(FakeDataStore, self).__init__()
    self.subjects = {}

    # All access to the store must hold this lock.
    self.lock = threading.RLock()
    # The set of all transactions in flight.
    self.transactions = {}

  def _Encode(self, value):
    """Encode the value into a Binary BSON object.

    The data store only supports the following values:
      -Integer
      -Unicode
      -Bytes (python string)

    We preserve integers and unicode objects, but serialize anything else.

    Args:
       value: The value to be encoded.

    Returns:
      An encoded value.
    """
    if isinstance(value, (basestring, int, float)):
      return value

    try:
      return value.SerializeToDataStore()
    except AttributeError:
      try:
        return value.SerializeToString()
      except AttributeError:
        return utils.SmartStr(value)

  @utils.Synchronized
  def DeleteSubject(self, subject, sync=False, token=None):
    _ = sync
    subject = utils.SmartUnicode(subject)
    self.security_manager.CheckDataStoreAccess(token, [subject], "w")
    try:
      del self.subjects[subject]
    except KeyError:
      pass

  @utils.Synchronized
  def Clear(self):
    self.subjects = {}

  def Transaction(self, subject, lease_time=None, token=None):
    return FakeTransaction(self, subject, lease_time=lease_time, token=token)

  @utils.Synchronized
  def Set(self,
          subject,
          attribute,
          value,
          timestamp=None,
          token=None,
          replace=True,
          sync=True):
    """Set the value into the data store."""
    subject = utils.SmartUnicode(subject)

    _ = sync
    self.security_manager.CheckDataStoreAccess(token, [subject], "w")

    attribute = utils.SmartUnicode(attribute)

    if timestamp is None or timestamp == self.NEWEST_TIMESTAMP:
      timestamp = time.time() * 1000000

    if subject not in self.subjects:
      self.subjects[subject] = {}

    if replace or attribute not in self.subjects[subject]:
      self.subjects[subject][attribute] = []

    self.subjects[subject][attribute].append([self._Encode(value),
                                              int(timestamp)])
    self.subjects[subject][attribute].sort(key=lambda x: x[1])

  @utils.Synchronized
  def MultiSet(self,
               subject,
               values,
               timestamp=None,
               token=None,
               replace=True,
               sync=True,
               to_delete=None):
    subject = utils.SmartUnicode(subject)
    self.security_manager.CheckDataStoreAccess(token, [subject], "w")

    if to_delete:
      self.DeleteAttributes(subject, to_delete, token=token)

    for k, seq in values.items():
      for v in seq:
        if isinstance(v, (list, tuple)):
          v, element_timestamp = v
        else:
          element_timestamp = timestamp

        self.Set(subject,
                 k,
                 v,
                 timestamp=element_timestamp,
                 token=token,
                 replace=replace,
                 sync=sync)

  @utils.Synchronized
  def DeleteAttributes(self,
                       subject,
                       attributes,
                       start=None,
                       end=None,
                       token=None,
                       sync=None):
    _ = sync  # Unimplemented.
    self.security_manager.CheckDataStoreAccess(token, [subject], "w")

    if isinstance(attributes, basestring):
      raise ValueError(
          "String passed to DeleteAttributes (non string iterable expected).")

    subject = utils.SmartUnicode(subject)
    try:
      record = self.subjects[subject]
      keys_to_delete = []
      for name, values in record.iteritems():
        if name not in attributes:
          continue

        start = start or 0
        if end is None:
          end = (2**63) - 1  # sys.maxint
        new_values = []
        for value, timestamp in values:
          if not start <= timestamp <= end:
            new_values.append((value, int(timestamp)))

        if new_values:
          record[name] = new_values
        else:
          keys_to_delete.append(name)

      for key in keys_to_delete:
        record.pop(key)
    except KeyError:
      pass

  @utils.Synchronized
  def ScanAttributes(self,
                     subject_prefix,
                     attributes,
                     after_urn="",
                     max_records=None,
                     token=None,
                     relaxed_order=False):
    subject_prefix = utils.SmartStr(rdfvalue.RDFURN(subject_prefix))
    if subject_prefix[-1] != "/":
      subject_prefix += "/"
    if after_urn:
      after_urn = utils.SmartUnicode(after_urn)
    self.security_manager.CheckDataStoreAccess(token, [subject_prefix], "qr")
    subjects = []
    for s in self.subjects.keys():
      if s.startswith(subject_prefix) and s > after_urn:
        subjects.append(s)
    subjects.sort()

    return_count = 0
    for s in subjects:
      if max_records and return_count >= max_records:
        break
      r = self.subjects[s]
      results = {}
      for attribute in attributes:
        attribute_list = r.get(attribute)
        if attribute_list:
          value, timestamp = attribute_list[-1]
          results[attribute] = (timestamp, value)
      if results:
        return_count += 1
        yield (s, results)

  @utils.Synchronized
  def ResolveMulti(self,
                   subject,
                   attributes,
                   timestamp=None,
                   limit=None,
                   token=None):
    subject = utils.SmartUnicode(subject)
    self.security_manager.CheckDataStoreAccess(
        token, [subject], self.GetRequiredResolveAccess(attributes))

    # Does timestamp represent a range?
    if isinstance(timestamp, (list, tuple)):
      start, end = timestamp  # pylint: disable=unpacking-non-sequence
    else:
      start, end = -1, 1 << 65

    start = int(start)
    end = int(end)

    if isinstance(attributes, str):
      attributes = [attributes]

    try:
      record = self.subjects[subject]
    except KeyError:
      return

    # Holds all the attributes which matched. Keys are attribute names, values
    # are lists of timestamped data.
    results = {}
    for attribute in attributes:
      for attr, values in record.iteritems():
        if attr == attribute:
          for value, ts in values:
            results_list = results.setdefault(attribute, [])
            # If we are always after the latest ts we clear older ones.
            if (results_list and timestamp == self.NEWEST_TIMESTAMP and
                results_list[0][1] < ts):
              results_list = []
              results[attribute] = results_list

            # Timestamp outside the range, drop it.
            elif ts < start or ts > end:
              continue

            results_list.append((attribute, ts, value))

    # Return the results in the same order they requested.
    remaining_limit = limit
    for attribute in attributes:
      # This returns triples of (attribute_name, timestamp, data). We want to
      # sort by timestamp.
      for _, ts, data in sorted(
          results.get(attribute, []), key=lambda x: x[1], reverse=True):
        if remaining_limit:
          remaining_limit -= 1
          if remaining_limit == 0:
            yield (attribute, data, ts)
            return

        yield (attribute, data, ts)

  @utils.Synchronized
  def MultiResolvePrefix(self,
                         subjects,
                         attribute_prefix,
                         token=None,
                         timestamp=None,
                         limit=None):
    required_access = self.GetRequiredResolveAccess(attribute_prefix)

    unicode_to_orig = {utils.SmartUnicode(s): s for s in subjects}
    result = {}
    for unicode_subject, orig_subject in unicode_to_orig.iteritems():

      # If any of the subjects is forbidden we fail the entire request.
      self.security_manager.CheckDataStoreAccess(token, [unicode_subject],
                                                 required_access)

      values = self.ResolvePrefix(
          unicode_subject,
          attribute_prefix,
          token=token,
          timestamp=timestamp,
          limit=limit)

      if not values:
        continue

      if limit:
        if limit < len(values):
          values = values[:limit]
        result[orig_subject] = values
        limit -= len(values)
        if limit <= 0:
          return result.iteritems()
      else:
        result[orig_subject] = values

    return result.iteritems()

  @utils.Synchronized
  def ResolvePrefix(self,
                    subject,
                    attribute_prefix,
                    token=None,
                    timestamp=None,
                    limit=None):
    """Resolve all attributes for a subject starting with a prefix."""
    subject = utils.SmartUnicode(subject)

    self.security_manager.CheckDataStoreAccess(
        token, [subject], self.GetRequiredResolveAccess(attribute_prefix))

    if timestamp in [None, self.NEWEST_TIMESTAMP, self.ALL_TIMESTAMPS]:
      start, end = 0, (2**63) - 1
    # Does timestamp represent a range?
    elif isinstance(timestamp, (list, tuple)):
      start, end = timestamp  # pylint: disable=unpacking-non-sequence
    else:
      raise ValueError("Invalid timestamp: %s" % timestamp)

    start = int(start)
    end = int(end)

    if isinstance(attribute_prefix, str):
      attribute_prefix = [attribute_prefix]

    subject = utils.SmartUnicode(subject)
    try:
      record = self.subjects[subject]
    except KeyError:
      return []

    # Holds all the attributes which matched. Keys are attribute names, values
    # are lists of timestamped data.
    results = {}
    nr_results = 0
    for prefix in attribute_prefix:
      for attribute, values in record.iteritems():
        if limit and nr_results >= limit:
          break
        if utils.SmartStr(attribute).startswith(prefix):
          for value, ts in values:
            results_list = results.setdefault(attribute, [])
            # If we are always after the latest ts we clear older ones.
            if (results_list and timestamp == self.NEWEST_TIMESTAMP and
                results_list[0][1] < ts):
              results_list = []
              results[attribute] = results_list

            # Timestamp outside the range, drop it.
            elif ts < start or ts > end:
              continue

            results_list.append((attribute, ts, value))
            nr_results += 1
            if limit and nr_results >= limit:
              break

    result = []
    for attribute_name, values in sorted(results.items()):
      # Values are triples of (attribute_name, timestamp, data). We want to
      # sort by timestamp.
      for _, ts, data in sorted(values, key=lambda x: x[1], reverse=True):
        # Return triples (attribute_name, data, timestamp).
        result.append((attribute_name, data, ts))
    return result

  def Size(self):
    total_size = sys.getsizeof(self.subjects)
    for subject, record in self.subjects.iteritems():
      total_size += sys.getsizeof(subject)
      total_size += sys.getsizeof(record)
      for attribute, values in record.iteritems():
        total_size += sys.getsizeof(attribute)
        total_size += sys.getsizeof(values)
        for value, timestamp in values:
          total_size += sys.getsizeof(value)
          total_size += sys.getsizeof(timestamp)
    return total_size

  def PrintSubjects(self, literal=None):
    for s in sorted(self.subjects):
      if literal and literal not in s:
        continue
      print s
