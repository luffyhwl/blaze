import ctypes
import llvm.core as lc
from llvm.core import Type, Function, Module
from .. import llvm_array as lla
from ..py2help import izip, _strtypes, c_ssize_t, PY2
from ..llvm_array import (void_type, intp_type,
                array_kinds, check_array,
                get_cpp_template, array_type, const_intp, LLArray, orderchar)
from .llutil import (int32_type, int8_p_type, single_ckernel_func_type,
                map_llvm_to_ctypes)
from ..ckernel import (ExprSingleOperation, JITKernelData,
                UnboundCKernelFunction)

def jit_compile_unbound_single_ckernel(bek):
    """Creates an UnboundCKernelFunction with the ExprSingleOperation prototype.

    Parameters
    ----------
    bek : BlazeElementKernel
        The blaze kernel to compile into an unbound single ckernel.
    """
    module = bek.module.clone()
    single_ck_func_name = bek.func.name +"_single_ckernel"
    single_ck_func = Function.new(module, single_ckernel_func_type,
                                      name=single_ck_func_name)
    block = single_ck_func.append_basic_block('entry')
    builder = lc.Builder.new(block)
    dst_ptr_arg, src_ptr_arr_arg, extra_ptr_arg = single_ck_func.args
    dst_ptr_arg.name = 'dst_ptr'
    src_ptr_arr_arg.name = 'src_ptrs'
    extra_ptr_arg.name = 'extra_ptr'
    # Build up the kernel data structure. Currently, this means
    # adding a shape field for each array argument. First comes
    # the kernel data prefix with a spot for the 'owner' reference added.
    input_field_indices = []
    kernel_data_fields = [Type.struct([int8_p_type]*3)]
    kernel_data_ctypes_fields = [('base', JITKernelData)]
    for i, (kind, a) in enumerate(izip(bek.kinds, bek.argtypes)):
        if isinstance(kind, tuple):
            if kind[0] != lla.C_CONTIGUOUS:
                raise ValueError('only support C contiguous array presently')
            input_field_indices.append(len(kernel_data_fields))
            kernel_data_fields.append(Type.array(
                            intp_type, len(bek.dshapes[i])-1))
            kernel_data_ctypes_fields.append(('operand_%d' % i,
                            c_ssize_t * (len(bek.dshapes[i])-1)))
        elif kind in [lla.SCALAR, lla.POINTER]:
            input_field_indices.append(None)
        else:
            raise TypeError(("unbound_single_ckernel codegen doesn't " +
                            "support the parameter kind %r yet") % (k,))
    # Make an LLVM and ctypes type for the extra data pointer.
    kernel_data_llvmtype = Type.struct(kernel_data_fields)
    class kernel_data_ctypestype(ctypes.Structure):
        _fields_ = kernel_data_ctypes_fields
    # Cast the extra pointer to the right llvm type
    extra_struct = builder.bitcast(extra_ptr_arg,
                    Type.pointer(kernel_data_llvmtype))
    # Convert the src pointer args to the
    # appropriate kinds for the llvm call
    args = []
    for i, (kind, atype) in enumerate(izip(bek.kinds[:-1], bek.argtypes)):
        if kind == lla.SCALAR:
            src_ptr = builder.bitcast(builder.load(
                            builder.gep(src_ptr_arr_arg,
                                    (lc.Constant.int(intp_type, i),))),
                                Type.pointer(atype))
            src_val = builder.load(src_ptr)
            args.append(src_val)
        elif kind == lla.POINTER:
            src_ptr = builder.bitcast(builder.load(
                            builder.gep(src_ptr_arr_arg,
                                    (lc.Constant.int(intp_type, i),))),
                                Type.pointer(atype))
            args.append(src_ptr)
        elif isinstance(kind, tuple):
            src_ptr = builder.bitcast(builder.load(
                            builder.gep(src_ptr_arr_arg,
                                    (lc.Constant.int(intp_type, i),))),
                                Type.pointer(kind[2]))
            # First get the shape of this parameter. This will
            # be a combination of Fixed and TypeVar (Var unsupported
            # here for now)
            shape = bek.dshapes[i][:-1]
            # Get the llvm array
            arr_var = builder.alloca(atype.pointee)
            builder.store(src_ptr,
                            builder.gep(arr_var,
                            (lc.Constant.int(int32_type, 0),
                             lc.Constant.int(int32_type, 0))))
            for j, sz in enumerate(shape):
                if isinstance(sz, Fixed):
                    # If the shape is already known at JIT compile time,
                    # insert the constant
                    shape_el_ptr = builder.gep(arr_var,
                                    (lc.Constant.int(int32_type, 0),
                                     lc.Constant.int(int32_type, 1),
                                     lc.Constant.int(intp_type, j)))
                    builder.store(lc.Constant.int(intp_type,
                                            operator.index(sz)),
                                    shape_el_ptr)
                elif isinstance(sz, TypeVar):
                    # TypeVar types are only known when the kernel is bound,
                    # so copy it from the extra data pointer
                    sz_from_extra_ptr = builder.gep(extra_struct,
                                    (lc.Constant.int(int32_type, 0),
                                     lc.Constant.int(int32_type,
                                            input_field_indices[i]),
                                     lc.Constant.int(intp_type, j)))
                    sz_from_extra = builder.load(sz_from_extra_ptr)
                    shape_el_ptr = builder.gep(arr_var,
                                    (lc.Constant.int(int32_type, 0),
                                     lc.Constant.int(int32_type, 1),
                                     lc.Constant.int(intp_type, j)))
                    builder.store(sz_from_extra, shape_el_ptr)
                else:
                    raise TypeError(("unbound_single_ckernel codegen doesn't " +
                                    "support dimension type %r") % type(sz))
            args.append(arr_var)
    # Call the function and store in the dst
    kind = bek.kinds[-1]
    func = module.get_function_named(bek.func.name)
    if kind == lla.SCALAR:
        dst_ptr = builder.bitcast(dst_ptr_arg,
                        Type.pointer(bek.return_type))
        dst_val = builder.call(func, args)
        builder.store(dst_val, dst_ptr)
    elif kind == lla.POINTER:
        dst_ptr = builder.bitcast(dst_ptr_arg,
                        Type.pointer(bek.return_type))
        builder.call(func, args + [dst_ptr])
    elif isinstance(kind, tuple):
        dst_ptr = builder.bitcast(dst_ptr_arg,
                        Type.pointer(kind[2]))
        # First get the shape of the output. This will
        # be a combination of Fixed and TypeVar (Var unsupported
        # here for now)
        shape = bek.dshapes[-1][:-1]
        # Get the llvm array
        arr_var = builder.alloca(bek.argtypes[-1].pointee)
        builder.store(dst_ptr,
                        builder.gep(arr_var,
                            (lc.Constant.int(int32_type, 0),
                            lc.Constant.int(int32_type, 0))))
        for j, sz in enumerate(shape):
            if isinstance(sz, Fixed):
                # If the shape is already known at JIT compile time,
                # insert the constant
                shape_el_ptr = builder.gep(arr_var,
                                (lc.Constant.int(int32_type, 0),
                                 lc.Constant.int(int32_type, 1),
                                 lc.Constant.int(intp_type, j)))
                builder.store(lc.Constant.int(intp_type,
                                        operator.index(sz)),
                                shape_el_ptr)
            elif isinstance(sz, TypeVar):
                # TypeVar types are only known when the kernel is bound,
                # so copy it from the extra data pointer
                sz_from_extra_ptr = builder.gep(extra_struct,
                                (lc.Constant.int(int32_type, 0),
                                 lc.Constant.int(int32_type,
                                        input_field_indices[-1]),
                                 lc.Constant.int(intp_type, j)))
                sz_from_extra = builder.load(sz_from_extra_ptr)
                shape_el_ptr = builder.gep(arr_var,
                                (lc.Constant.int(int32_type, 0),
                                 lc.Constant.int(int32_type, 1),
                                 lc.Constant.int(intp_type, j)))
                builder.store(sz_from_extra, shape_el_ptr)
            else:
                raise TypeError(("unbound_single_ckernel codegen doesn't " +
                                "support dimension type %r") % type(sz))
        builder.call(func, args + [arr_var])
    else:
        raise TypeError(("single_ckernel codegen doesn't " +
                        "support kind %r") % kind)
    builder.ret_void()

    #print("Function before optimization passes:")
    #print(single_ck_func)
    #module.verify()

    import llvm.ee as le
    from llvm.passes import build_pass_managers
    tm = le.TargetMachine.new(opt=3, cm=le.CM_JITDEFAULT, features='')
    pms = build_pass_managers(tm, opt=3, fpm=False,
                    vectorize=True, loop_vectorize=True)
    pms.pm.run(module)

    #print("Function after optimization passes:")
    #print(single_ck_func)

    # DEBUGGING: Verify the module.
    #module.verify()
    # TODO: Cache the EE - the interplay with the func_ptr
    #       was broken, so just avoiding caching for now
    # FIXME: Temporarily disabling AVX, because of misdetection
    #        in linux VMs. Some code is in llvmpy's workarounds
    #        submodule related to this.
    ee = le.EngineBuilder.new(module).mattrs("-avx").create()
    func_ptr = ee.get_pointer_to_function(single_ck_func)
    # Create a function which copies the shape from data
    # descriptors to the extra data struct.
    if len(kernel_data_ctypes_fields) == 1:
        def bind_func(estruct, dst_dd, src_dd_list):
            pass
    else:
        def bind_func(estruct, dst_dd, src_dd_list):
            for i, (ds, dd) in enumerate(
                            izip(bek.dshapes, src_dd_list + [dst_dd])):
                shape = [operator.index(dim)
                                for dim in dd.dshape[-len(ds):-1]]
                cshape = getattr(estruct, 'operand_%d' % i)
                for j, dim_size in enumerate(shape):
                    cshape[j] = dim_size

    return UnboundCKernelFunction(
                    ExprSingleOperation(func_ptr),
                    kernel_data_ctypestype,
                    bind_func,
                    (ee, func_ptr))