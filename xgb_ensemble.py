import xgboost as xgb
import numpy as np
from scipy import sparse
from sklearn.utils import shuffle

import os
import argparse
from time import time

from baseline_utils import SUPERBORO_CODE, create_dir
from obtain_features import *
from bridge_info import BRIDGES
from borough_labels import BOROUGHS


parser = argparse.ArgumentParser(
    description="Integrates trained XGBoost models to "
        "do inference on cross-superboro data points."
    )

# Super-boro models for inference
# NOTE: The models whose names are specified below are not
#       part of the repo; to run this code, first use
#       `baseline_model.py` script to train and save each
#       super-borough model
BEST_MODEL_PATHS = {
    "111": ["models/sb11_sm1.0_111_2019-12-06-04-08-05.xgb",
            "models/sb22_sm1.0_111_2019-12-09-19-16-01.xgb",    # Val Loss: 336.6702
            "models/sb33_sm1.0_111_2019-12-08-21-01-14.xgb",],
    "001": ["models/sb11_sm1.0_001_2019-12-06-11-22-49.xgb",
            "models/sb22_sm1.0_001_2019-12-09-19-16-24.xgb",
            "models/sb33_sm1.0_001_2019-12-08-21-02-35.xgb",],
    "110": ["models/sb11_sm1.0_110_2019-12-10-01-28-42.xgb",
            "models/sb22_sm1.0_110_2019-12-09-18-57-34.xgb",    # Val Loss: 335.2700
            "models/sb33_sm1.0_110_2019-12-09-18-49-37.xgb",],  # Val Loss: 589.2293
}

parser.add_argument("-sb1", "--sb1-model-path", type=str,
                    help="Path to the stored model for Super-boro 1 (MEBx), "
                         "will be automatically set")
parser.add_argument("-sb2", "--sb2-model-path", type=str,
                    help="Path to the stored model for Super-boro 2 (BkQ), "
                         "will be automatically set")
parser.add_argument("-sb3", "--sb3-model-path", type=str,
                    help="Path to the stored model for Super-boro 3 (St), "
                         "will be automatically set")

# Dataset
parser.add_argument("-sm", "--stddev-mul", type=float,
                    default=1, choices=[-1,0.25,0.5,1,2],
                    help="Number of stddev to add to the cutoff "
                         "for outlier removal. -1 gives the whole dataset "
                         "(choices: -1,0.25,0.5,1,2, default=1, "
                         "assumed to have at most one decimal place)")
parser.add_argument("--db-path", type=str, default="./rides.db",
                    help="Path to the sqlite3 database file.")
parser.add_argument("-r", "--rand-subset", type=int, default=0,
                    help="Shuffle the dataset, then sample a subset "
                         "with size specified by argument (default: 0). "
                         "Size 0 means the whole dataset is used (i.e. variant='all')")
parser.add_argument("-doh", "--datetime-one-hot", action="store_true",
                    help="Let the date & time features be loaded as one-hot")
parser.add_argument("-woh", "--weekdays-one-hot", action="store_true",
                    help="Let the week-of-the-day feature be loaded as one-hot")
parser.add_argument("--no-loc-id", dest='loc_id', action="store_false",
                    help="Let the zone IDs be excluded from the dataset")
parser.add_argument("--test-size", type=float, default=1.0,
                    help="Proportion of test set (default: 1.0)")

# Preprocessing
parser.add_argument("--use-saved", action="store_true",
                    help="Use the preprocessed & saved features & outputs. "
                         "Need to first run with '--save'. "
                         "Options `--woh`, `--doh`, and `--no-loc-id` have "
                         "to be specified the same way as when the data has "
                         "been saved")
parser.add_argument("--save", action="store_true",
                    help="Save the np/sparse array containing cross-superboro "
                         "trips to disk")

# Misc
parser.add_argument("-v", "--verbose", action='count', default=0,
                    help="Let the model print training progress (if supported)")
parser.add_argument("--log-dir", type=str, default="logs/xgb_ensemble",
                    help="Directory to store test log in")
parser.add_argument("--log", type=int, default=0,
                    help="Log testing progress every {log} test points "
                         "(default: 0 => do not log)")


def connecting_bridges(start_superboro_id, end_superboro_id, conn):
    """Returns all bridges connecting two superboroughs

    :start_superboro_id: the first superborough we wish our
        bridges to connect
    :end_superboro_id: the other superborough we wish our
        bridges to connect
    :conn: a connection to the database containing information
        on our bridges
    :returns: a list of tuples, where the tuples contain two integers,
        representing a start and an end location id
    """
    cursor = conn.cursor()

    start_superboro_string = list_to_quoted_string(SUPERBORO_CODE[start_superboro_id])
    end_superboro_string = list_to_quoted_string(SUPERBORO_CODE[end_superboro_id])

    query = f"""
    SELECT b.LocationID1, b.LocationID2
    FROM bridges b, locations l1, locations l2
    WHERE b.LocationID1 = l1.LocationID
    AND b.LocationID2 = l2.LocationID
    AND ((l1.Borough IN ({start_superboro_string}) AND l2.Borough in ({end_superboro_string}))
    OR (l2.Borough IN ({start_superboro_string}) AND l1.Borough in ({end_superboro_string})))
    """

    try:
        cursor.execute(query)
    except Error as e:
        print(e)
    rows = cursor.fetchall()
    return rows


def get_location_ids_in_boro(boro, conn):
    cursor = conn.cursor()

    query = f"""
    SELECT LocationID
    FROM locations
    WHERE Borough = "{boro}"
    """

    try:
        cursor.execute(query)
    except Error as e:
        print(e)
    rows = cursor.fetchall()
    return set([row[0] for row in rows])


def crossboro_preproc_setup(conn):
    loaded_bridge_data = {}
    superboro_location_ids = {}
    boro_location_ids = {}
    inverted_boro_dict = {v: k for k, v in BOROUGHS.items()}
    coordinates = extract_all_coordinates(conn, 'coordinates')
    conn = conn

    def crossboro_preproc(features, doh, woh, loc_id):
        """Given the details of a single cross-superboro trip
        as a 1-D array of features, for each bridge connecting
        the two superboros involved, return the following info:
            - PU Super-boro code (int between 1 and 3)
            - DO Super-boro code (int between 1 and 3)
            - Features for PU->bridge (np.array)
            - Features for bridge->DO (np.array)

        :features: A single row in `features` array obtained
            from `extract_features` function call
            (corresponds to a single cross-superboro trip)
        :doh: Boolean for datetime-one-hotness
        :woh: Boolean for weekdays-one-hotness
        :loc_id: Boolean for including PU, DO locationIDs
            (locationIDs are one-hot if included)
        :returns: A list with length equal to the number of
            bridges between the two super-boros, where each
            element is a sublist with the info listed above
        """
        nonlocal loaded_bridge_data
        nonlocal inverted_boro_dict
        nonlocal superboro_location_ids
        nonlocal boro_location_ids
        nonlocal coordinates
        nonlocal conn

        # NOTE: if `features` is a row from `scipy.sparse` matrix,
        #       it may have to be converted into `np.array` (dense)
        is_sparse = doh or woh or loc_id
        indices = features.nonzero()[1]

        boro_start_index = 9
        if woh:
            boro_start_index += 6
        if doh:
            boro_start_index += 182

        boro_start_from_end = -4
        boro_end_from_end = -3
        if not loc_id:
            boro_start_from_end += 2
            boro_end_from_end += 2

        start_boro_one_hot = indices[boro_start_from_end] - boro_start_index
        start_boro = inverted_boro_dict[start_boro_one_hot]
        end_boro_one_hot = indices[boro_end_from_end] - (boro_start_index + 6)
        end_boro = inverted_boro_dict[end_boro_one_hot]
        start_code = 0
        end_code = 0

        for k, v in SUPERBORO_CODE.items():
            if v is None:
                continue

            if start_boro in v:
                start_code = k
            if end_boro in v:
                end_code = k

        superboro_pair_code = tuple(sorted((start_code, end_code)))
        if superboro_pair_code not in loaded_bridge_data:
            loaded_bridge_data[superboro_pair_code] = connecting_bridges(start_code, end_code, conn)

        if start_code not in superboro_location_ids:
            location_ids = set()
            for boro in SUPERBORO_CODE[start_code]:
                if boro not in boro_location_ids:
                    boro_locations = get_location_ids_in_boro(boro, conn)
                    boro_location_ids[boro] = boro_locations
                location_ids.update(boro_location_ids[boro])
            superboro_location_ids[start_code] = location_ids

        if end_code not in superboro_location_ids:
            location_ids = set()
            for boro in SUPERBORO_CODE[end_code]:
                if boro not in boro_location_ids:
                    boro_locations = get_location_ids_in_boro(boro, conn)
                    boro_location_ids[boro] = boro_locations
                location_ids.update(boro_location_ids[boro])
            superboro_location_ids[end_code] = location_ids

        rides = []
        for bridge in loaded_bridge_data[superboro_pair_code]:
            first_zone, second_zone = bridge
            start_zone = None
            end_zone = None

            if first_zone in superboro_location_ids[start_code]:
                start_zone = first_zone
                end_zone = second_zone
            else:
                start_zone = second_zone
                end_zone = first_zone

            bridge_start_boro = None
            bridge_end_boro = None
            for k, v in boro_location_ids.items():
                if start_zone in v:
                    bridge_start_boro = k
                if end_zone in v:
                    bridge_end_boro = k

            bridge_start_boro_id = BOROUGHS[bridge_start_boro]
            bridge_end_boro_id = BOROUGHS[bridge_end_boro]

            first_leg = features.copy()
            first_leg[0, indices[boro_end_from_end]] = 0
            first_leg[0, bridge_start_boro_id + boro_start_index + 6] = 1
            first_leg[0, boro_start_index - 1] = coordinates[start_zone][0]
            first_leg[0, boro_start_index] = coordinates[start_zone][1]

            second_leg = features.copy()
            second_leg[0, indices[boro_start_from_end]] = 0
            second_leg[0, bridge_end_boro_id + boro_start_index] = 1
            second_leg[0, boro_start_index - 3] = coordinates[end_zone][0]
            second_leg[0, boro_start_index - 2] = coordinates[end_zone][1]

            rides.append((start_code, end_code, first_leg, second_leg))

        return rides

    return crossboro_preproc


def load_models(args):
    """Returns a list containing XGBoost models to
    conduct cross-super-boro inferences with.
    The `None` at the beginning is added as a padding,
    to match model indices with superboro codes.

    :args: argparse Namespace
    :returns: List containing a `None` at index 0,
        followed by three XGBoost models
    """
    sb1_model = xgb.Booster(model_file=args.sb1_model_path)
    sb2_model = xgb.Booster(model_file=args.sb2_model_path)
    sb3_model = xgb.Booster(model_file=args.sb3_model_path)
    return [None,sb1_model,sb2_model,sb3_model]


def evaluate(models, features, outputs, doh, woh, loc_id, args):
    """Evaluate the selected superboro models on cross-superboro
    trips.

    :models: List of models for prediction (starting at index 1)
    :features, outputs: Cross-superboro dataset to evaluate upon
    :doh: Boolean for datetime-one-hotness
    :woh: Boolean for weekdays-one-hotness
    :loc_id: Boolean for including PU, DO locationIDs
        (locationIDs are one-hot if included)
    :returns: Total loss from the given dataset
    """
    conn = create_connection(args.db_path)
    total_loss = 0
    crossboro_preproc = crossboro_preproc_setup(conn)
    convert = lambda trip: crossboro_preproc(trip, doh, woh, loc_id)

    if args.log > 0:
        log_dir = create_dir(args.log_dir)
        log_path = os.path.join(log_dir,
                                f"log_ts{args.test_size}"
                                f"_sm{args.stddev_mul}"
                                f"_{int(args.datetime_one_hot)}"
                                f"{int(args.weekdays_one_hot)}"
                                f"{int(args.loc_id)}"
                                ".txt")
        print(f">>> To be logged in: {log_path}")
        try:
            os.remove(log_path)
        except OSError:
            pass
        with open(log_path, "a+") as log_file:
            log_file.write(f"sb1: {args.sb1_model_path} \n")
            log_file.write(f"sb2: {args.sb2_model_path} \n")
            log_file.write(f"sb3: {args.sb3_model_path} \n")
            log_file.write("\n")

    breakpoint = args.log if args.log > 0 else 10

    # Iterate through each cross-superboro trip
    for idx, (inputs_, output) in enumerate(zip(features, outputs)):
        inputs = convert(inputs_)

        min_duration = 1e20
        max_duration = -1

        # Compute loss for each bridge and record minimum
        for (sb_PU, sb_DO, f_PU, f_DO) in inputs:
            f_PU_dmat = xgb.DMatrix(f_PU)
            f_DO_dmat = xgb.DMatrix(f_DO)

            # Compute durations for each trip
            PU_duration = models[sb_PU].predict(f_PU_dmat)[0]
            DO_duration = models[sb_DO].predict(f_DO_dmat)[0]

            # Compute total duration & MSE loss
            duration = PU_duration + DO_duration
            min_duration = min(min_duration, duration)
            max_duration = max(max_duration, duration)

        # For debugging purposes
        if args.verbose > 1:
            print(f"[{idx+1:8}] "
                  f"min: {min_duration:15.3f}, "
                  f"max: {max_duration:15.3f}, "
                  f"Target: {output}")

            if min_duration == 1e20:
                print(inputs_)
                print(inputs)

        total_loss += (min_duration - output)**2

        if (idx+1) % breakpoint == 0:
            if args.verbose > 0:
                print(f">>> Running test point {idx+1}, "
                      f"current loss {np.sqrt(total_loss / (idx+1)):.4f}")
            if args.log > 0:
                with open(log_path, "a+") as log_file:
                    log_file.write(f"idx {idx+1}: "
                        f"{np.sqrt(total_loss / (idx+1)):.4f}\n")

    return np.sqrt(total_loss / outputs.shape[0])


def load_cross_superboro(args, f_path=None, o_path=None):
    """Load cross-superboro datapoints from DB into memory,
    with features as `scipy.sparse.csr_matrix` or `np.array`
    and outputs as `np.array`.
    Iterates through each pair of distinct combinations of
    super-boroughs and stacks the corresponding features and
    outputs together.
    Saves the stacked features and outputs to disk if `--save`
    option has been specified.

    :args: argparse Namespace
    :f_path, o_path: Paths to store feaetures and outputs at
        (needed when `--save` option is on)
    :returns: features and outputs arrays containing all
        cross-superboro trips
    """
    conn = create_connection(args.db_path)

    is_sparse = args.datetime_one_hot \
                or args.weekdays_one_hot \
                or args.loc_id
    features = None
    outputs = None

    if args.verbose:
        start_time = time()

    for start_sb, end_sb in ([1,2],[1,3],[2,3]):
        if args.verbose:
            print(f">>> Working on SBs {start_sb} and {end_sb}...")

        data_params = {
            "table_name":"rides",
            "variant":"random" if args.rand_subset > 0 else "all",
            "size":args.rand_subset,
            "datetime_onehot":args.datetime_one_hot,
            "weekdays_onehot":args.weekdays_one_hot,
            "include_loc_ids":args.loc_id,
            "start_super_boro":SUPERBORO_CODE[start_sb],
            "end_super_boro":SUPERBORO_CODE[end_sb],
            "stddev_multiplier":args.stddev_mul,
        }

        extracted_features, extracted_outputs = \
            extract_features(conn, **data_params)

        if args.verbose:
            print(">>> Extraction complete, "
                  f"# of rows: {extracted_outputs.shape[0]}")

        if features is None and outputs is None:
            features = extracted_features
            outputs = extracted_outputs
        else:
            if is_sparse:
                features = sparse.vstack([features, extracted_features],
                                         format="csr")
            else:
                features = np.vstack([features, extracted_features])
            outputs = np.concatenate([outputs, extracted_outputs])

    if args.verbose:
        print(">>> All pairs complete, "
              f"# of rows: {outputs.shape[0]}, "
              f"total duration: {time() - start_time:.2f} seconds")

    if args.save:
        assert os.path.split(f_path)[0] == os.path.split(o_path)[0], \
            "ERROR: 'features' and 'outputs' should be saved " \
            "in the same location"
        create_dir(os.path.split(f_path)[0])
        assert f_path is not None and o_path is not None, \
            "ERROR: Please provide `f_path` and `o_path` arguments"
        if is_sparse:
            sparse.save_npz(f_path, features)
        else:
            np.save(f_path, features)
        np.save(o_path, outputs)

        if args.verbose:
            print(">>> Features and outputs saved to disk")

    return features, outputs


def main():
    args = parser.parse_args()
    is_sparse = args.datetime_one_hot \
                or args.weekdays_one_hot \
                or args.loc_id
    f_path = "./data/crossboro_" \
             f"{int(args.datetime_one_hot)}" \
             f"{int(args.weekdays_one_hot)}" \
             f"{int(args.loc_id)}" \
             "_features"
    o_path = "./data/crossboro_" \
             f"{int(args.datetime_one_hot)}" \
             f"{int(args.weekdays_one_hot)}" \
             f"{int(args.loc_id)}" \
             "_outputs.npy"

    models_key = f"{int(args.datetime_one_hot)}" \
                 f"{int(args.weekdays_one_hot)}" \
                 f"{int(args.loc_id)}"
    args.sb1_model_path, args.sb2_model_path, args.sb3_model_path = \
        BEST_MODEL_PATHS[models_key]

    if args.use_saved:  # Load arrays stored in disk
        if args.verbose:
            start_time = time()

        features = sparse.load_npz(f_path + ".npz") if is_sparse \
                    else np.load(f_path + ".npy")
        outputs = np.load(o_path)

        if args.verbose:
            print(">>> Loading from disk complete, "
                  f"# of rows: {outputs.shape[0]}, "
                  f"total duration: {time() - start_time:.2f} seconds")

    else:   # Parse arrays from DB
        features, outputs = load_cross_superboro(args, f_path, o_path)

    n_samples = int(features.shape[0] * args.test_size)
    features, outputs = shuffle(features, outputs,
                                random_state=10701,
                                n_samples=n_samples)

    if args.verbose:
        print(f">>> features.shape = {features.shape}")
        print(f">>> outputs.shape = {outputs.shape}")

    models = load_models(args)

    if args.verbose:
        start_time = time()

    loss = evaluate(models, features, outputs,
                    args.datetime_one_hot,
                    args.weekdays_one_hot,
                    args.loc_id, args)
    if args.verbose:
        print(f">>> Total evaluation runtime: {time() - start_time}")

    print(f">>> Loss for cross-superboro trips: {loss}")


if __name__ == "__main__":
    main()
