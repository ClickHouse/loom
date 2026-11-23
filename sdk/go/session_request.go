package loom

import (
	"encoding"
	"encoding/json"
	"reflect"
)

// Protected requests accept bounded JSON data, not custom encoders with
// unbounded work or side effects. Unbound serialization keeps its existing API.
func validateProtectedInput(input any) error {
	nodes, bytes := 10000, maxProtectedBody
	return protectedInputValue(reflect.ValueOf(input), 0, &nodes, &bytes)
}

func protectedInputValue(value reflect.Value, depth int, nodes, bytes *int) error {
	*nodes -= 1
	*bytes -= 8
	if depth > 64 || *nodes < 0 || *bytes < 0 {
		return ErrSessionProtectionUnavailable
	}
	if !value.IsValid() {
		return nil
	}
	for _, encoder := range []reflect.Type{reflect.TypeOf((*json.Marshaler)(nil)).Elem(), reflect.TypeOf((*encoding.TextMarshaler)(nil)).Elem()} {
		if value.Type().Implements(encoder) || reflect.PointerTo(value.Type()).Implements(encoder) {
			return ErrSessionProtectionUnavailable
		}
	}
	switch value.Kind() {
	case reflect.Interface, reflect.Pointer:
		if !value.IsNil() {
			return protectedInputValue(value.Elem(), depth+1, nodes, bytes)
		}
	case reflect.Map:
		if value.Type().Key().Kind() != reflect.String || value.Len() > *nodes {
			return ErrSessionProtectionUnavailable
		}
		iterator := value.MapRange()
		for iterator.Next() {
			*bytes -= 6*len(iterator.Key().String()) + 4
			if err := protectedInputValue(iterator.Value(), depth+1, nodes, bytes); err != nil {
				return err
			}
		}
	case reflect.Struct:
		for i := 0; i < value.NumField(); i++ {
			field := value.Type().Field(i)
			if (field.PkgPath != "" && !field.Anonymous) || field.Tag.Get("json") == "-" {
				continue
			}
			*bytes -= 6*(len(field.Name)+len(field.Tag.Get("json"))) + 4
			if err := protectedInputValue(value.Field(i), depth+1, nodes, bytes); err != nil {
				return err
			}
		}
	case reflect.Slice, reflect.Array:
		if value.Len() > *nodes {
			return ErrSessionProtectionUnavailable
		}
		for i := 0; i < value.Len(); i++ {
			if err := protectedInputValue(value.Index(i), depth+1, nodes, bytes); err != nil {
				return err
			}
		}
	case reflect.String:
		// Six bytes cover the longest JSON escape of one input byte.
		if value.Len() > *bytes/6 {
			return ErrSessionProtectionUnavailable
		}
		*bytes -= 6*value.Len()
	case reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64,
		reflect.Float32, reflect.Float64:
		*bytes -= 32
	default:
		return ErrSessionProtectionUnavailable
	}
	if *bytes < 0 {
		return ErrSessionProtectionUnavailable
	}
	return nil
}
