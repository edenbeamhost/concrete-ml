"""Tests the encrypted data-frame API abd its coherence with Pandas"""

import copy
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

import numpy
import pandas
import pytest
from concrete.fhe.compilation.specs import ClientSpecs

import concrete.ml.pandas
from concrete.ml.pandas import ClientEngine, load_encrypted_dataframe
from concrete.ml.pandas._development import CLIENT_PATH, get_min_max_allowed, save_client_server
from concrete.ml.pytest.utils import pandas_dataframe_are_equal


def generate_pandas_dataframe(
    dtype: str = "mixed",
    feat_name: str = "feat",
    n_features: int = 1,
    index_name: Optional[str] = None,
    indexes: Optional[Union[int, List]] = None,
    index_position: int = 0,
    include_nan: bool = True,
    float_min: float = -10.0,
    float_max: float = 10.0,
) -> pandas.DataFrame:
    """Generate a Pandas data-frame.

    Note that in this case, the index is not the Pandas' index but rather a dedicated column.

    Args:
        dtype (str): The dtype to consider when generating the data-frame, one of
            ["int", "float", "str", "mixed"]:
            * "int": generates n_features feature(s) made of integers in the allowed range
            * "float": generates n_features feature(s) made of floating points
            * "str": generates n_features feature(s) made of strings picked from a fixed list
            * "mixed": generates 3*n_features features, n_features for each of the above dtypes
            Default to "mixed".
        feat_name (str): The features' base name to consider. Default to "feat".
        n_features (int): The number of features to use per dtype. Default to 1.
        index_name (Optional[str]): The index's name. Default to None ("index").
        indexes (Optional[Union[int, List]]): Custom indexes to consider. Default to None (5 rows,
            indexed from 1 to 5).
        index_position (int): The index's column position in the data-frame. Default to 0.
        include_nan (bool): If NaN values should be put in the data-frame. If True, they are
            inserted in the first row. Default to True.
        float_min (float): The minimum float value to use for defining the range of values allowed
            when generating the float column.
        float_max (float): The maximum float value to use for defining the range of values allowed
            when generating the float column.

    Returns:
        pandas.DataFrame: The generated Pandas data-frame.
    """
    if indexes is None:
        indexes = 5

    allowed_dtype = ["int", "float", "str", "mixed"]
    assert dtype in allowed_dtype, f"Parameter 'dtype' must be in {allowed_dtype}. Got {dtype}."
    assert isinstance(
        indexes, (int, list)
    ), f"Parameter 'indexes' must either be an int or a list. Got {type(indexes)}"
    assert not (
        include_nan and dtype == "int"
    ), "NaN values cannot be included when testing integers values"

    # Make sure 0 is not included in the index
    # Remove this once NaN values are not represented by 0 anymore
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    if isinstance(indexes, int):
        indexes = list(range(1, indexes + 1))

    if index_name is None:
        index_name = "index"

    columns = {}

    # Add a column with integer values
    if dtype in ["int", "mixed"]:
        low, high = get_min_max_allowed()

        for i in range(1, n_features + 1):
            columns[f"{feat_name}_int_{i}"] = list(
                numpy.random.randint(low=low, high=high, size=(len(indexes),))
            )

    # Add a column with float values (including NaN or not)
    if dtype in ["float", "mixed"]:
        for i in range(1, n_features + 1):
            column_name = f"{feat_name}_float_{i}"
            columns[column_name] = list(
                numpy.random.uniform(low=float_min, high=float_max, size=(len(indexes),))
            )

            if include_nan:
                columns[column_name][0] = numpy.nan

    # Add a column with string values (including NaN or not)
    if dtype in ["str", "mixed"]:
        str_values = ["apple", "orange", "watermelon", "cherry", "banana"]

        for i in range(1, n_features + 1):
            column_name = f"{feat_name}_str_{i}"
            columns[column_name] = list(numpy.random.choice(str_values, size=(len(indexes),)))

            if include_nan:
                columns[column_name][0] = numpy.nan

    pandas_dataframe = pandas.DataFrame(columns)

    assert index_position < len(pandas_dataframe.columns), (
        "Parameter 'index_position' should not be greater than the number of features. Got "
        f"{index_position=} for {len(pandas_dataframe.columns)} features."
    )

    # Insert the column on which to merge at the given position
    pandas_dataframe.insert(index_position, index_name, indexes)

    return pandas_dataframe


def get_two_encrypted_dataframes(
    feat_names: Optional[Sequence] = None,
    indexes_left: Optional[Union[int, List]] = None,
    indexes_right: Optional[Union[int, List]] = None,
    **data_kwargs,
) -> Tuple[pandas.DataFrame, pandas.DataFrame]:
    """Generated two Pandas data-frame.

    Args:
        feat_names (Optional[Sequence]): The features' base name to consider for both data-frame.
            Default to None (("left", "right")).
        indexes_left (Optional[Union[int, List]]): Custom indexes to consider for the first
            data-frame. Default to None.
        indexes_right (Optional[Union[int, List]]): Custom indexes to consider for the second
            data-frame. Default to None.

    Returns:
        Tuple[pandas.DataFrame, pandas.DataFrame]: The two generated Pandas data-frame.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        keys_path = Path(temp_dir) / "keys"

        client_1 = ClientEngine(keys_path=keys_path)
        client_2 = ClientEngine(keys_path=keys_path)

    if feat_names is None:
        feat_names = ("left", "right")

    pandas_df_left = generate_pandas_dataframe(
        feat_name=feat_names[0], indexes=indexes_left, **data_kwargs
    )
    pandas_df_right = generate_pandas_dataframe(
        feat_name=feat_names[1], indexes=indexes_right, **data_kwargs
    )

    encrypted_df_left = client_1.encrypt_from_pandas(pandas_df_left)
    encrypted_df_right = client_2.encrypt_from_pandas(pandas_df_right)

    return encrypted_df_left, encrypted_df_right


@pytest.mark.parametrize("as_method", [True, False])
@pytest.mark.parametrize("how", ["left", "right"])
@pytest.mark.parametrize("selected_column", ["index", None])
def test_merge(as_method, how, selected_column):
    """Test that the encrypted merge operator is equivalent to Pandas' merge."""
    pandas_kwargs = {"how": how, "on": selected_column}

    with tempfile.TemporaryDirectory() as temp_dir:
        keys_path = Path(temp_dir) / "keys"

        client_1 = ClientEngine(keys_path=keys_path)
        client_2 = ClientEngine(keys_path=keys_path)

    pandas_df_left = generate_pandas_dataframe(
        feat_name="left", index_name=selected_column, indexes=[1, 2, 3, 4], index_position=2
    )
    pandas_df_right = generate_pandas_dataframe(
        feat_name="right", index_name=selected_column, indexes=[2, 3], index_position=1
    )

    encrypted_df_left = client_1.encrypt_from_pandas(pandas_df_left)
    encrypted_df_right = client_2.encrypt_from_pandas(pandas_df_right)

    # If we test the '.merge' method
    if as_method:
        pandas_joined_df = pandas_df_left.merge(pandas_df_right, **pandas_kwargs)
        encrypted_df_joined = encrypted_df_left.merge(encrypted_df_right, **pandas_kwargs)

    else:
        pandas_joined_df = pandas.merge(pandas_df_left, pandas_df_right, **pandas_kwargs)
        encrypted_df_joined = concrete.ml.pandas.merge(
            encrypted_df_left, encrypted_df_right, **pandas_kwargs
        )

    clear_df_joined_1 = client_1.decrypt_to_pandas(encrypted_df_joined)
    clear_df_joined_2 = client_2.decrypt_to_pandas(encrypted_df_joined)

    assert pandas_dataframe_are_equal(
        clear_df_joined_1, clear_df_joined_2, equal_nan=True
    ), "Joined encrypted data-frames decrypted by different clients are not equal."

    # Improve the test to avoid risk of flaky
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    assert pandas_dataframe_are_equal(
        clear_df_joined_1, pandas_joined_df, float_atol=1, equal_nan=True
    ), "Joined encrypted data-frame does not match Pandas' joined data-frame."


@pytest.mark.parametrize("dtype", ["int", "float", "str", "mixed"])
def test_pre_post_processing(dtype):
    """Test pre-processing and post-processing steps."""
    include_nan = dtype != "int"

    client = ClientEngine()

    pandas_df = generate_pandas_dataframe(dtype=dtype, include_nan=include_nan)

    encrypted_df = client.encrypt_from_pandas(pandas_df)

    clear_df = client.decrypt_to_pandas(encrypted_df)

    # Improve the test to avoid risk of flaky
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    assert pandas_dataframe_are_equal(
        pandas_df, clear_df, float_atol=1, equal_nan=include_nan
    ), "Processed encrypted data-frame does not match Pandas' initial data-frame."


@pytest.mark.parametrize("float_min_max", [0.0, 1.0])
def test_quantization_corner_cases(float_min_max):
    """Test quantization process for corner cases.

    This test makes sure that the pre-process and post-process steps properly handle columns with
    single float values (0 or else), as the quantization process handle these differently.
    """

    client = ClientEngine()

    pandas_df = generate_pandas_dataframe(
        dtype="float", float_min=float_min_max, float_max=float_min_max
    )

    encrypted_df = client.encrypt_from_pandas(pandas_df)

    clear_df = client.decrypt_to_pandas(encrypted_df)

    # Improve the test to avoid risk of flaky
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    assert pandas_dataframe_are_equal(
        pandas_df, clear_df, float_atol=1, equal_nan=True
    ), "Processed encrypted data-frame does not match Pandas' initial data-frame."


def test_save_load():
    """Test saving and loading an encrypted data-frame."""
    client = ClientEngine()

    pandas_df = generate_pandas_dataframe()

    encrypted_df = client.encrypt_from_pandas(pandas_df)

    with tempfile.TemporaryDirectory() as temp_dir:
        enc_df_path = Path(temp_dir) / "encrypted_dataframe"

        encrypted_df.save(enc_df_path)

        loaded_encrypted_df = load_encrypted_dataframe(enc_df_path)

    assert (
        encrypted_df.api_version == loaded_encrypted_df.api_version
    ), "API versions between initial and loaded encrypted data-frame do not match."

    assert (
        encrypted_df.column_names == loaded_encrypted_df.column_names
    ), "Column names between initial and loaded encrypted data-frame do not match."

    assert (
        encrypted_df.column_names_to_position == loaded_encrypted_df.column_names_to_position
    ), "Column name mappings between initial and loaded encrypted data-frame do not match."

    assert (
        encrypted_df.dtype_mappings == loaded_encrypted_df.dtype_mappings
    ), "Dtype mappings between initial and loaded encrypted data-frame do not match."

    loaded_clear_df = client.decrypt_to_pandas(loaded_encrypted_df)

    # Improve the test to avoid risk of flaky
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    assert pandas_dataframe_are_equal(
        loaded_clear_df, pandas_df, float_atol=1, equal_nan=True
    ), "Loaded encrypted data-frame does not match the initial encrypted data-frame."


def check_invalid_merge_parameters():
    """Check that unsupported or invalid parameters for merge raise correct errors."""
    encrypted_df_left, encrypted_df_right = get_two_encrypted_dataframes()

    unsupported_pandas_parameters_and_values = [
        ("left_on", "index"),
        ("right_on", "index"),
        ("left_index", True),
        ("right_index", True),
        ("sort", True),
        ("copy", True),
        ("indicator", True),
        ("validate", "1:1"),
    ]

    for parameter, unsupported_value in unsupported_pandas_parameters_and_values:
        with pytest.raises(
            ValueError,
            match=f"Parameter '{parameter}' is not currently supported. Got {unsupported_value}.",
        ):
            encrypted_df_left.merge(
                encrypted_df_right,
                **{parameter: unsupported_value},
            )

    for how in ["outer", "inner", "cross"]:
        with pytest.raises(
            NotImplementedError,
            match=re.escape(f"Merge type '{how}' is not currently implemented."),
        ):
            encrypted_df_left.merge(
                encrypted_df_right,
                how=how,
            )


def check_no_multi_columns_merge():
    """Check that trying to merge on several columns raise the correct error."""
    encrypted_df_left, encrypted_df_right = get_two_encrypted_dataframes(feat_names=("", ""))

    with pytest.raises(
        ValueError,
        match="Merging on 0 or several columns is not currently available.",
    ):
        encrypted_df_left.merge(encrypted_df_right)


def check_column_coherence():
    """Check that merging data-frames with unsupported scheme raises correct errors."""
    index_name = "index"

    # Test when a selected column has a different dtype than the other one
    encrypted_df_left, encrypted_df_right = get_two_encrypted_dataframes(
        index_name=index_name, indexes_left=[1, 2], indexes_right=[1.3, 7.3]
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            f"Dtypes of both common column '{index_name}' do not match. Got int64 (left) and "
            "float64 (right)."
        ),
    ):
        encrypted_df_left.merge(encrypted_df_right)

    # Test when both selected columns have a float dtype
    encrypted_df_left, encrypted_df_right = get_two_encrypted_dataframes(
        index_name=index_name, indexes_left=[1.3, 7.3], indexes_right=[1.3, 7.3]
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            f"Column '{index_name}' cannot be selected for merging both data-frames because it has "
            f"a floating dtype (float64)"
        ),
    ):
        encrypted_df_left.merge(encrypted_df_right)

    # Test when both selected columns have a object dtype (string) but with different string
    # mappings
    encrypted_df_left, encrypted_df_right = get_two_encrypted_dataframes(
        index_name=index_name,
        indexes_left=["cherry", "watermelon"],
        indexes_right=["orange", "watermelon"],
    )

    with pytest.raises(
        ValueError,
        match=re.escape(
            f"Mappings for string values in both common column '{index_name}' do not match."
        ),
    ):
        encrypted_df_left.merge(encrypted_df_right)


def check_unsupported_input_values():
    """Check that initializing a data-frame with unsupported inputs raises correct errors."""
    client = ClientEngine()

    # Test with integer values that are out of bound
    indexes_high_integers = [73, 100]
    pandas_df = generate_pandas_dataframe(indexes=indexes_high_integers)

    with pytest.raises(
        ValueError,
        match=".* contains values that are out of bounds. Expected values to be in interval.*",
    ):
        client.encrypt_from_pandas(pandas_df)

    # Test with string values that contains too many unique values
    indexes_str = list(map(str, list(range(100))))
    pandas_df = generate_pandas_dataframe(indexes=indexes_str)

    with pytest.raises(ValueError, match=".* contains too many unique values.*"):
        client.encrypt_from_pandas(pandas_df)

    # Test with object dtype that contains non-string values
    indexes_object_non_str = [object(), object()]
    pandas_df = generate_pandas_dataframe(indexes=indexes_object_non_str)

    with pytest.raises(
        ValueError,
        match=".* contains non-string values, which is not currently supported.*",
    ):
        client.encrypt_from_pandas(pandas_df)

    # Test with values of unsupported dtype
    indexes_unsupported_dtype = [1 + 2j, -3 - 4j]
    pandas_df = generate_pandas_dataframe(indexes=indexes_unsupported_dtype)

    with pytest.raises(
        ValueError,
        match=".* has dtype 'complex128', which is not currently supported.",
    ):
        client.encrypt_from_pandas(pandas_df)

    # Test with a data-frame that contains an Pandas index with possible relevant information in it
    indexes_not_range = [1, 3]
    pandas_df = generate_pandas_dataframe(indexes=indexes_not_range)
    pandas_df.set_index("index", inplace=True)

    with pytest.raises(
        ValueError,
        match=(
            "The data-frame's index has not been reset. Please make sure to not put relevant data "
            "in the index and instead store it in a dedicated column. Encrypted data-frames do not "
            "currently support any index-based operations."
        ),
    ):
        client.encrypt_from_pandas(pandas_df)


def check_post_processing_coherence():
    """Check post-processing a data-frame with unsupported scheme raises correct errors."""
    index_name = "index"

    client = ClientEngine()

    pandas_df = generate_pandas_dataframe(index_name=index_name)

    encrypted_df = client.encrypt_from_pandas(pandas_df)

    wrong_dtype = "complex128"
    encrypted_df.dtype_mappings[index_name]["dtype"] = wrong_dtype

    with pytest.raises(
        ValueError,
        match=re.escape(
            f"Column '{index_name}' has dtype '{wrong_dtype}', which is unexpected and thus not "
            "supported."
        ),
    ):
        client.decrypt_to_pandas(encrypted_df)


def test_error_raises():
    """Check that expected errors are properly raised."""
    check_invalid_merge_parameters()
    check_no_multi_columns_merge()
    check_column_coherence()
    check_unsupported_input_values()
    check_post_processing_coherence()
    check_invalid_schema_format()
    check_invalid_schema_values()


def deserialize_client_file(client_path: Union[Path, str]) -> ClientSpecs:
    """Deserialize a Concrete client file.

    Args:
        client_path (Union[Path, str]): The path to the client file.

    Returns:
        ClientSpecs: The ClientSpecs object used for instantiating a Client object.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        output_dir_path = Path(temp_dir)

        shutil.unpack_archive(client_path, output_dir_path, "zip")

        with (output_dir_path / "client.specs.json").open("rb") as f:
            client_specs = ClientSpecs.deserialize(f.read())

        return client_specs


def concrete_client_files_are_equal(
    client_path_1: Union[Path, str], client_path_2: Union[Path, str]
) -> bool:
    """Deserialize and compare two Concrete client files.

    Args:
        client_path_1 (Union[Path, str]): The path to the first client file.
        client_path_2 (Union[Path, str]): The path to the second client file.

    Returns:
        bool: If both client files are equal.
    """
    client_path_1, client_path_2 = Path(client_path_1), Path(client_path_2)

    assert client_path_1.is_file(), f"Path '{client_path_1}' is not a file."
    assert client_path_2.is_file(), f"Path '{client_path_2}' is not a file."

    client_specs_1 = deserialize_client_file(client_path_1)
    client_specs_2 = deserialize_client_file(client_path_2)

    return client_specs_1 == client_specs_2


# Improve this test if Concrete Python provides an official way to check such compatibility
# FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
def test_parameter_sets():
    """Test if new generated parameter sets (client.zip) are equal to the ones stored in source."""
    with tempfile.TemporaryDirectory() as temp_dir:
        client_path = Path(temp_dir) / "client.zip"
        server_path = Path(temp_dir) / "server.zip"

        save_client_server(client_path=client_path, server_path=server_path)

        assert concrete_client_files_are_equal(
            client_path, CLIENT_PATH
        ), "The new generated client file is not equal to the one stored in source."


def test_print_and_repr():
    """Test that print, repr and get_schema properly work."""
    pandas_df = pandas.DataFrame(
        {"index": [1, 2], "A": [9, 3], "B": [-5.2, 2.9], "C": ["orange", "watermelon"]}
    )

    client = ClientEngine()

    encrypted_df = client.encrypt_from_pandas(pandas_df)

    # Because values are encrypted and this cannot be seeded, we are currently not able to make sure
    # the print and repr are matching an expected result
    print(encrypted_df)
    repr(encrypted_df)
    encrypted_df._repr_html_()  # pylint: disable=protected-access

    expected_schema = pandas.DataFrame(
        {
            "index": ["int64", numpy.nan, numpy.nan, numpy.nan],
            "A": ["int64", numpy.nan, numpy.nan, numpy.nan],
            "B": ["float64", 1.7283950617283952, -9.987654320987655, numpy.nan],
            "C": ["object", numpy.nan, numpy.nan, {"orange": 1, "watermelon": 2}],
        },
        index=["dtype", "scale", "zero_point", "str_to_int"],
    )

    schema = encrypted_df.get_schema()

    assert pandas_dataframe_are_equal(
        expected_schema, schema, equal_nan=True
    ), "Expected and retrieved schemas do not match."


def get_input_schema(pandas_dataframe, selected_schema=None):
    """Get a data-frame's expected input schema."""
    schema = {}
    for column_name in pandas_dataframe.columns:
        column = pandas_dataframe[column_name]
        if numpy.issubdtype(column.dtype, numpy.floating):
            schema[column_name] = {
                "min": column.min(),
                "max": column.max(),
            }

        elif column.dtype == "object":
            unique_values = column.unique()

            # Only take strings into account and thus avoid NaN values
            schema[column_name] = {
                val: i for i, val in enumerate(unique_values) if isinstance(val, str)
            }

    # Update the common column's mapping
    if selected_schema is not None:
        schema.update(selected_schema)

    return schema


def test_schema_input():
    """Test that users can properly provide schemas when encrypting data-frames."""
    selected_column = "index"
    pandas_kwargs = {"how": "left", "on": selected_column}

    with tempfile.TemporaryDirectory() as temp_dir:
        keys_path = Path(temp_dir) / "keys"

        client_1 = ClientEngine(keys_path=keys_path)
        client_2 = ClientEngine(keys_path=keys_path)

    indexes_left = ["one", "two", "three", "four"]
    indexes_right = ["two", "three"]

    schema_index = {selected_column: {"one": 1, "two": 2, "three": 3, "four": 4}}

    pandas_df_left = generate_pandas_dataframe(
        feat_name="left", index_name=selected_column, indexes=indexes_left, index_position=2
    )
    pandas_df_right = generate_pandas_dataframe(
        feat_name="right", index_name=selected_column, indexes=indexes_right, index_position=1
    )

    schema_left = get_input_schema(pandas_df_left, selected_schema=schema_index)
    schema_right = get_input_schema(pandas_df_right, selected_schema=schema_index)

    encrypted_df_left = client_1.encrypt_from_pandas(pandas_df_left, schema=schema_left)
    encrypted_df_right = client_2.encrypt_from_pandas(pandas_df_right, schema=schema_right)

    pandas_joined_df = pandas_df_left.merge(pandas_df_right, **pandas_kwargs)
    encrypted_df_joined = encrypted_df_left.merge(encrypted_df_right, **pandas_kwargs)

    clear_df_joined_1 = client_1.decrypt_to_pandas(encrypted_df_joined)
    clear_df_joined_2 = client_2.decrypt_to_pandas(encrypted_df_joined)

    assert pandas_dataframe_are_equal(
        clear_df_joined_1, clear_df_joined_2, equal_nan=True
    ), "Joined encrypted data-frames decrypted by different clients are not equal."

    # Improve the test to avoid risk of flaky
    # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4342
    assert pandas_dataframe_are_equal(
        clear_df_joined_1, pandas_joined_df, float_atol=1, equal_nan=True
    ), "Joined encrypted data-frame does not match Pandas' joined data-frame."


def check_invalid_schema_format():
    """Check that encrypting data-frames with an unsupported schema format raises correct errors."""
    selected_column = "index"

    with tempfile.TemporaryDirectory() as temp_dir:
        keys_path = Path(temp_dir) / "keys"

        client = ClientEngine(keys_path=keys_path)

    pandas_df = generate_pandas_dataframe(index_name=selected_column)

    with pytest.raises(
        ValueError,
        match="When set, parameter 'schema' must be a dictionary.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=[])

    schema_wrong_column = {"wrong_column": None}

    with pytest.raises(
        ValueError,
        match="Column name '.*' found in the given schema cannot be found.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_wrong_column)

    schema_wrong_mapping_type = {selected_column: [None]}

    with pytest.raises(
        ValueError,
        match="Mapping for column '.*' is not a dictionary. .*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_wrong_mapping_type)


def check_invalid_schema_values():
    """Check that encrypting data-frames with an unsupported schema values raises correct errors."""
    selected_column = "index"
    feat_name = "feat"
    float_min = -10.0
    float_max = 10.0

    with tempfile.TemporaryDirectory() as temp_dir:
        keys_path = Path(temp_dir) / "keys"

        client = ClientEngine(keys_path=keys_path)

    pandas_df = generate_pandas_dataframe(
        feat_name=feat_name, index_name=selected_column, float_min=float_min, float_max=float_max
    )

    schema_int_column = {f"{feat_name}_int_1": {None: None}}

    with pytest.raises(
        ValueError,
        match="Column '.*' contains integer values and therefore does not.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_int_column)

    schema_float_column = {f"{feat_name}_float_1": {"wrong_mapping": 1.0}}

    with pytest.raises(
        ValueError,
        match="Column '.*' contains float values but the associated mapping.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_float_column)

    schema_float_oob = {f"{feat_name}_float_1": {"min": float_min // 2, "max": float_max // 2}}

    with pytest.raises(
        ValueError,
        match=r"Column '.*' \(dtype=float64\) contains values that are out of bounds.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_float_oob)

    string_column = f"{feat_name}_str_1"

    schema_string_nan = {string_column: {numpy.NaN: 1}}

    with pytest.raises(
        ValueError,
        match="String mapping for column '.*' contains numpy.NaN as a key.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_string_nan)

    schema_string_missing_values = {string_column: {"apple": 1}}

    with pytest.raises(
        ValueError,
        match="String mapping keys for column '.*' are not considering all values.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_string_missing_values)

    # Retrieve the string column's unique values and create a mapping, except for numpy.NaN values
    string_values = pandas_df[string_column].unique()
    string_values = [
        string_value for string_value in string_values if isinstance(string_value, str)
    ]
    string_mapping = {val: i for i, val in enumerate(string_values)}

    string_mapping_non_int = copy.copy(string_mapping)

    # Disable mypy as this type assignment is expected for the error to be raised
    string_mapping_non_int[string_values[0]] = "orange"  # type: ignore[assignment]

    schema_string_non_int = {string_column: string_mapping_non_int}

    with pytest.raises(
        ValueError,
        match="String mapping values for column '.*' must be integers.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_string_non_int)

    string_mapping_oob = copy.copy(string_mapping)
    string_mapping_oob[string_values[0]] = -1

    schema_string_oob = {string_column: string_mapping_oob}

    with pytest.raises(
        ValueError,
        match="String mapping values for column '.*' are out of bounds.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_string_oob)

    string_mapping_non_unique = copy.copy(string_mapping)
    string_mapping_non_unique[string_values[0]] = 1
    string_mapping_non_unique[string_values[1]] = 1

    schema_string_non_unique = {string_column: string_mapping_non_unique}

    with pytest.raises(
        ValueError,
        match="String mapping values for column '.*' must be unique.*",
    ):
        client.encrypt_from_pandas(pandas_df, schema=schema_string_non_unique)
